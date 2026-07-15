import copy
import importlib.util
import hashlib
import json
import datetime as dt
from pathlib import Path

import duckdb
import pytest


path = Path(__file__).with_name("rfq_full_stage.py")
spec = importlib.util.spec_from_file_location("rfq_full_stage", path)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mod)

repair04_path = Path(__file__).with_name("repair04_registration.py")
repair04_spec = importlib.util.spec_from_file_location(
    "repair04_registration_for_rfq_consumer_tests", repair04_path
)
repair04_producer = importlib.util.module_from_spec(repair04_spec)
assert repair04_spec.loader is not None
repair04_spec.loader.exec_module(repair04_producer)

repair05_path = Path(__file__).with_name("repair05_registration.py")
repair05_spec = importlib.util.spec_from_file_location(
    "repair05_registration_for_rfq_consumer_tests", repair05_path
)
repair05_producer = importlib.util.module_from_spec(repair05_spec)
assert repair05_spec.loader is not None
repair05_spec.loader.exec_module(repair05_producer)

repair05_test_path = Path(__file__).with_name("test_repair05_registration.py")
repair05_test_spec = importlib.util.spec_from_file_location(
    "repair05_registration_fixture_for_rfq_consumer", repair05_test_path
)
repair05_test_helpers = importlib.util.module_from_spec(repair05_test_spec)
assert repair05_test_spec.loader is not None
repair05_test_spec.loader.exec_module(repair05_test_helpers)

repair06_path = Path(__file__).with_name("repair06_registration.py")
repair06_spec = importlib.util.spec_from_file_location(
    "repair06_registration_for_rfq_consumer_tests", repair06_path
)
repair06_producer = importlib.util.module_from_spec(repair06_spec)
assert repair06_spec.loader is not None
repair06_spec.loader.exec_module(repair06_producer)

repair06_test_path = Path(__file__).with_name("test_repair06_registration.py")
repair06_test_spec = importlib.util.spec_from_file_location(
    "repair06_registration_fixture_for_rfq_consumer", repair06_test_path
)
repair06_test_helpers = importlib.util.module_from_spec(repair06_test_spec)
assert repair06_test_spec.loader is not None
repair06_test_spec.loader.exec_module(repair06_test_helpers)


def recorder(wall_ns, frame=None, *, mono_ns=None, epoch=1, marker=None):
    row = {
        "recv_wall_ns": wall_ns,
        "recv_mono_ns": mono_ns if mono_ns is not None else wall_ns,
        "stream_epoch": epoch,
    }
    if frame is not None:
        row["raw"] = json.dumps(frame, separators=(",", ":"))
    elif marker is not None:
        # RawLogWriter always emits the field; control markers carry an exact
        # empty string rather than an absent/null payload.
        row["raw"] = ""
    if marker is not None:
        row["marker"] = marker
    return row


def frame(kind, rid, market, timestamp, **extra):
    field = "created_ts" if kind == "rfq_created" else "deleted_ts"
    msg = {"id": rid, "creator_id": extra.pop("creator_id", ""),
           "market_ticker": market, field: timestamp}
    msg.update(extra)
    return {"type": kind, "sid": 7, "msg": msg}


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def repair03_consumer_fixture(tmp_path, monkeypatch):
    """Build a small but fully cross-bound repair-03 preregistration."""
    run_dir = tmp_path / "synthetic-repair03-consumer"
    run_dir.mkdir()
    applied_at = "2026-07-15T16:00:00Z"
    selection = "4" * 64
    retained_set = "2" * 64
    quarantined_set = "3" * 64
    full_set = "1" * 64
    cumulative = [
        {"key": "raw_rfq/a", "sha256": "a" * 64, "size": 10},
        {"key": "raw_rfq/b", "sha256": "b" * 64, "size": 30},
    ]
    repair01 = {"repair_id": "repair-01"}
    parent_receipt = (
        "DATA_INTEGRITY/repairs/repair-02/REPAIR_REGISTRATION.json"
    )
    write_json(run_dir / parent_receipt, {"repair_id": "repair-02"})
    core_results = [{"path": "REPORT/tables/core.json", "sha256": "9" * 64}]
    cycle_binding = {"schema_version": "synthetic-cycle1-binding-v1"}
    repair02 = {
        "repair_id": "repair-02",
        "repair_receipt_path": parent_receipt,
        "repair_receipt_sha256": mod.sha256(run_dir / parent_receipt),
        "cumulative_quarantined_objects": copy.deepcopy(cumulative),
        "cycle1_duckdb_binding": copy.deepcopy(cycle_binding),
        "core_result_artifacts": copy.deepcopy(core_results),
    }
    repair_chain = [
        {"repair_id": "repair-01", "registration_sha256": "5" * 64},
        {"repair_id": "repair-02", "registration_sha256": "6" * 64},
    ]
    failed_bindings = [
        {"repair_id": "repair-01", "attempt_id": "attempt-01"},
        {"repair_id": "repair-02", "attempt_id": "attempt-02"},
    ]
    inputs = {
        "objects": 4,
        "logical_manifest_bindings": 4,
        "bytes": 100,
        "path_size_fingerprint_sha256": full_set,
    }
    base_result = {
        **inputs,
        "coverage_status": "PARTIAL_OBJECT_COVERAGE_QUARANTINED",
        "full_object_coverage": False,
        "unique_objects_total": 4,
        "unique_bytes_total": 100,
        "consumed_unique_objects": 2,
        "consumed_logical_bindings": 2,
        "consumed_bytes": 60,
        "consumed_object_set_sha256": retained_set,
        "quarantined_unique_objects": 2,
        "quarantined_logical_bindings": 2,
        "quarantined_bytes": 40,
        "quarantined_object_set_sha256": quarantined_set,
        "selection_fingerprint_sha256": selection,
        "repair_chain": copy.deepcopy(repair_chain),
        "failed_attempt_bindings": copy.deepcopy(failed_bindings),
    }

    pre_root = run_dir / mod.REPAIR03_PRE_ROOT
    repair_root = run_dir / mod.REPAIR03_ROOT
    post_root = repair_root / "post_repair"
    previous_commit = "c" * 40
    current_commit = "d" * 40
    query_relative = "queries/rfq_full_stage.py"
    query_payload = b"PARSER_CONTRACT = 'repair-03'\n"
    old_query_payload = b"PARSER_CONTRACT = 'repair-02'\n"
    write_bytes = {
        "SOURCE_MANIFEST.json": b'{"source":"repair-03"}\n',
        "SOURCE_SHA256SUMS.txt": b"e" * 64 + b"  source.py\n",
        query_relative: query_payload,
    }
    query_sha = hashlib.sha256(query_payload).hexdigest()
    write_bytes["QUERY_SHA256SUMS.txt"] = (
        f"{query_sha}  {query_relative}\n".encode("utf-8")
    )
    old_bytes = {
        "SOURCE_MANIFEST.json": b'{"source":"repair-02"}\n',
        "SOURCE_SHA256SUMS.txt": b"f" * 64 + b"  source.py\n",
        query_relative: old_query_payload,
    }
    old_query_sha = hashlib.sha256(old_query_payload).hexdigest()
    old_bytes["QUERY_SHA256SUMS.txt"] = (
        f"{old_query_sha}  {query_relative}\n".encode("utf-8")
    )
    for relative, payload in write_bytes.items():
        path = run_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        post = post_root / relative
        post.parent.mkdir(parents=True, exist_ok=True)
        post.write_bytes(payload)
    for relative, payload in old_bytes.items():
        path = pre_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    def repository_identity(commit, source_manifest, source_sums, query_sums):
        return {
            "execution_commit": commit,
            "source_manifest_sha256": hashlib.sha256(source_manifest).hexdigest(),
            "source_sha256s_sha256": hashlib.sha256(source_sums).hexdigest(),
            "query_set_sha256": hashlib.sha256(query_sums).hexdigest(),
            "query_files": [query_relative],
        }

    first_identity = repository_identity(
        "a" * 40, b"first-manifest", b"first-sums", b"first-query"
    )
    second_identity = repository_identity(
        "b" * 40, b"second-manifest", b"second-sums", b"second-query"
    )
    previous_identity = repository_identity(
        previous_commit,
        old_bytes["SOURCE_MANIFEST.json"],
        old_bytes["SOURCE_SHA256SUMS.txt"],
        old_bytes["QUERY_SHA256SUMS.txt"],
    )
    current_identity = repository_identity(
        current_commit,
        write_bytes["SOURCE_MANIFEST.json"],
        write_bytes["SOURCE_SHA256SUMS.txt"],
        write_bytes["QUERY_SHA256SUMS.txt"],
    )
    previous_repository = {
        **copy.deepcopy(previous_identity),
        "identity_history": [first_identity, second_identity, previous_identity],
    }
    repository = {
        **copy.deepcopy(previous_repository),
        **copy.deepcopy(current_identity),
        "previous_execution_commit": previous_commit,
        "previous_source_manifest_sha256": previous_identity[
            "source_manifest_sha256"
        ],
        "previous_source_sha256s_sha256": previous_identity[
            "source_sha256s_sha256"
        ],
        "previous_query_set_sha256": previous_identity["query_set_sha256"],
        "previous_identity": previous_identity,
        "registration_repair_id": "repair-03",
        "identity_history": [
            first_identity, second_identity, previous_identity, current_identity,
        ],
    }
    pre_manifest = {
        "run_id": run_dir.name,
        "status": "CYCLE1_CORE_COMPLETE_RFQ_QUARANTINE_REPAIR02_REGISTERED",
        "registration_state": (
            "RE_FROZEN_AFTER_DATA_INTEGRITY_REPAIR02_BEFORE_RFQ_RESULT"
        ),
        "repository": previous_repository,
        "data_integrity_repairs": [repair01, repair02],
    }
    write_json(pre_root / "RUN_MANIFEST.json", pre_manifest)

    pre_registry = b'{"trial_registration_id":"prior"}\n'
    (pre_root / "TRIAL_REGISTRY.jsonl").write_bytes(pre_registry)
    attestation = {
        "schema_version": mod.REPAIR03_SOURCE_ATTESTATION_SCHEMA,
        "run_id": run_dir.name,
        "mission_sha256": mod.EXPECTED_MISSION_SHA256,
        "parent_execution_commit": previous_commit,
        "execution_commit": current_commit,
        "direct_parent_verified": True,
        "git_tree": "e" * 40,
        "source_relative": mod.REPAIR03_SOURCE_RELATIVE,
        "source_tree_clean_at_attestation": True,
        "source_manifest_sha256": current_identity["source_manifest_sha256"],
        "source_sha256s_sha256": current_identity["source_sha256s_sha256"],
        "commit_changed_paths": copy.deepcopy(mod.REPAIR03_CHANGED_PATHS),
        "attested_at_utc": "2026-07-15T15:59:00Z",
    }
    write_json(run_dir / mod.REPAIR03_SOURCE_ATTESTATION, attestation)
    write_json(pre_root / mod.REPAIR03_SOURCE_ATTESTATION, attestation)

    canonical_audit_source = b"# synthetic audited parser\n"
    for relative in (
        mod.REPAIR03_AUDIT_SOURCE,
        mod.REPAIR03_AUDIT_SOURCE_ARCHIVE,
        "tmp/rfq_inner_payload_contract_audit03.py",
    ):
        path = run_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_audit_source)
    audit_source_sha = hashlib.sha256(canonical_audit_source).hexdigest()
    preserved = run_dir / mod.REPAIR03_PRESERVED_SCRATCH
    preserved.parent.mkdir(parents=True, exist_ok=True)
    preserved.write_bytes(b"preserved attempt-03 scratch")
    preserved_sha = mod.sha256(preserved)
    expected_repair02_resource = {
        "label": mod.REPAIR02_SUCCESS_RESOURCE_LABEL,
        "path": mod.REPAIR02_SUCCESS_RESOURCE_PATH,
    }
    failed_state = {
        "schema": "rfq-full-stage-state-v1",
        "run_id": run_dir.name,
        "status": "FAILED_DATA_INTEGRITY_REQUIRES_NEW_PREREGISTRATION",
        "resume": False,
        "next_required_authority": (
            "EXPLICIT_NEW_PREREGISTRATION_OR_RFQ_SCOPE_TERMINATION"
        ),
        "input_fingerprint": selection,
        "error": (
            "unexpected malformed inner RFQ payload in a consumed object; aborting"
        ),
        "expected_success_resource": expected_repair02_resource,
    }
    failed_resource = {
        "schema_version": "w09-stage-resource-v1",
        "label": mod.REPAIR02_SUCCESS_RESOURCE_LABEL,
        "return_code": 1,
        "command": ["python3", "/source/rfq_full_stage.py"],
    }
    consumed_objects = [
        {
            "key": "raw_rfq/retained-a.ndjson",
            "size": 20,
            "sha256": "7" * 64,
            "bound_release_ids": [mod.RELEASE_IDS[0]],
        },
        {
            "key": "raw_rfq/retained-b.ndjson",
            "size": 40,
            "sha256": "8" * 64,
            "bound_release_ids": [mod.RELEASE_IDS[1]],
        },
    ]
    failed_input = {
        "schema": "rfq-full-input-identity-v2",
        "run_id": run_dir.name,
        "selection_fingerprint_sha256": selection,
        "repair_chain": repair_chain,
        "failed_attempt_bindings": failed_bindings,
        "expected_success_resource": expected_repair02_resource,
        "consumed_objects": consumed_objects,
    }
    failed_scratch = {
        "schema_version": "rfq-failed-scratch-receipt-v1",
        "run_id": run_dir.name,
        "input_fingerprint": selection,
        "disposition": "PRESERVED_RENAMED_NO_RESUME",
        "resume_allowed": False,
        "preserved_scratch_path": str(preserved.resolve()),
        "bytes": preserved.stat().st_size,
        "sha256": preserved_sha,
    }
    audited_sha = mod._compact_json_sha256(mod.REPAIR03_AUDITED_PARSER_CONTRACT)
    marker_counts = {"hour_open": 1, "segment_receipt": 1}
    object_summaries = [
        {
            "key": consumed_objects[0]["key"],
            "expected_size": consumed_objects[0]["size"],
            "expected_sha256": consumed_objects[0]["sha256"],
            "observed_size": consumed_objects[0]["size"],
            "observed_sha256": consumed_objects[0]["sha256"],
            "identity_match": True,
            "total_lines": 1,
            "expected_empty_control_marker_rows": 1,
            "data_frame_rows": 0,
            "segment_receipt_rows": 0,
            "marker_counts": {"hour_open": 1},
            "outer_invalid_rows": 0,
            "unexpected_inner_payload_rows": 0,
            "unexpected_details": [],
            "raw_payload_redacted": True,
        },
        {
            "key": consumed_objects[1]["key"],
            "expected_size": consumed_objects[1]["size"],
            "expected_sha256": consumed_objects[1]["sha256"],
            "observed_size": consumed_objects[1]["size"],
            "observed_sha256": consumed_objects[1]["sha256"],
            "identity_match": True,
            "total_lines": 2,
            "expected_empty_control_marker_rows": 0,
            "data_frame_rows": 1,
            "segment_receipt_rows": 1,
            "marker_counts": {"segment_receipt": 1},
            "outer_invalid_rows": 0,
            "unexpected_inner_payload_rows": 0,
            "unexpected_details": [],
            "raw_payload_redacted": True,
        },
    ]
    audit = {
        "schema_version": "rfq-inner-payload-contract-audit-v1",
        "run_id": run_dir.name,
        "status": "COMPLETE_INNER_PAYLOAD_CONTRACT_AUDIT",
        "scope": "RETAINED_OBJECT_INNER_PAYLOAD_CONTRACT_ONLY_NO_RESEARCH_RESULT",
        "analysis_result_opened": False,
        "raw_payload_redacted": True,
        "input_fingerprint": selection,
        "input_identity_path": "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json",
        "expected_empty_control_marker_allowlist": list(
            mod.EXPECTED_BLANK_CONTROL_MARKERS
        ),
        "objects_scanned": 2,
        "bytes_scanned": 60,
        "lines_scanned": 3,
        "data_frame_rows": 1,
        "segment_receipt_rows": 1,
        "expected_empty_control_marker_rows": 1,
        "marker_counts": marker_counts,
        "identity_mismatch_count": 0,
        "identity_mismatches": [],
        "outer_invalid_object_count": 0,
        "outer_invalid_line_count": 0,
        "unexpected_inner_payload_object_count": 0,
        "unexpected_inner_payload_row_count": 0,
        "unexpected_inner_payload_objects": [],
        "json_object_payload_marker_contract": [
            "<marker field absent>", "segment_receipt",
        ],
        "parser_contract": copy.deepcopy(mod.REPAIR03_AUDITED_PARSER_CONTRACT),
        "parser_contract_sha256": audited_sha,
        "audit_script_path": "tmp/rfq_inner_payload_contract_audit03.py",
        "audit_script_sha256": audit_source_sha,
        "object_summaries": object_summaries,
        "started_at_utc": "2026-07-15T15:00:00Z",
        "completed_at_utc": "2026-07-15T15:10:00Z",
        "wall_seconds": 600.0,
        "workers": 8,
    }
    audit_resource = {
        "schema_version": "w09-stage-resource-v1",
        "label": "rfq_inner_payload_contract_audit03_final",
        "return_code": 0,
        "command": [
            "/opt/w09/venv/bin/python",
            (
                f"/srv/w09-research/runs/{run_dir.name}/"
                "tmp/rfq_inner_payload_contract_audit03.py"
            ),
            "--run-dir", f"/srv/w09-research/runs/{run_dir.name}",
            "--cache-root", "/srv/w09-research/cache", "--workers", "8",
        ],
    }
    paired_json = (
        ("REPORT/tables/RFQ_FULL_STAGE_STATE.json",
         mod.REPAIR03_FAILED_STATE_ARCHIVE, failed_state),
        ("logs/resources/rfq_full_stage_repair02.json",
         mod.REPAIR03_FAILED_RESOURCE_ARCHIVE, failed_resource),
        ("DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT_03.json",
         mod.REPAIR03_FAILED_SCRATCH_ARCHIVE, failed_scratch),
        ("DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json",
         mod.REPAIR03_FAILED_INPUT_ARCHIVE, failed_input),
        ("DATA_INTEGRITY/RFQ_INNER_PAYLOAD_CONTRACT_AUDIT_03.json",
         mod.REPAIR03_AUDIT_ARCHIVE, audit),
        ("logs/resources/rfq_inner_payload_contract_audit03_final.json",
         mod.REPAIR03_AUDIT_RESOURCE_ARCHIVE, audit_resource),
        (mod.CYCLE1_DUCKDB_BINDING,
         mod.REPAIR03_CYCLE1_BINDING_ARCHIVE, cycle_binding),
    )
    for active_relative, archive_relative, document in paired_json:
        write_json(run_dir / active_relative, document)
        write_json(run_dir / archive_relative, document)
    audit["input_identity_sha256"] = mod.sha256(
        run_dir / mod.REPAIR03_FAILED_INPUT_ARCHIVE
    )
    write_json(
        run_dir / "DATA_INTEGRITY/RFQ_INNER_PAYLOAD_CONTRACT_AUDIT_03.json",
        audit,
    )
    write_json(run_dir / mod.REPAIR03_AUDIT_ARCHIVE, audit)

    audit_sha = mod.sha256(run_dir / mod.REPAIR03_AUDIT_ARCHIVE)
    parser_contract = {
        "schema_version": mod.REPAIR03_PARSER_SCHEMA,
        "run_id": run_dir.name,
        "created_at_utc": applied_at,
        "finding": mod.REPAIR03_FINDING,
        "mission_sha256": mod.EXPECTED_MISSION_SHA256,
        "selection_fingerprint_sha256": selection,
        "retained_unique_objects": 2,
        "retained_bytes": 60,
        "outer_ndjson_policy": "STRICT_NDJSON_IGNORE_ERRORS_FALSE",
        "expected_blank_control_markers": list(
            mod.EXPECTED_BLANK_CONTROL_MARKERS
        ),
        "expected_blank_raw_representation": "EXACT_EMPTY_STRING",
        "non_control_payload_policy": "NONEMPTY_STRING_VALID_JSON_OBJECT_REQUIRED",
        "unexpected_payload_policy": "ABORT_BEFORE_RESULT",
        "line_salvage": False,
        "data_selection_change": False,
        "hypothesis_design_change": False,
        "registered_query_path": query_relative,
        "registered_query_sha256": query_sha,
        "audit_path": mod.REPAIR03_AUDIT_ARCHIVE,
        "audit_sha256": audit_sha,
    }
    write_json(run_dir / mod.REPAIR03_PARSER_CONTRACT, parser_contract)
    parser_sha = mod.sha256(run_dir / mod.REPAIR03_PARSER_CONTRACT)
    authority = {
        "schema_version": mod.REPAIR03_AUTHORITY_SCHEMA,
        "run_id": run_dir.name,
        "recorded_at_utc": applied_at,
        "mission_sha256": mod.EXPECTED_MISSION_SHA256,
        "authority_basis": "MISSION_AUTHORIZED_AUTONOMOUS_RESEARCH_CODE_CORRECTION",
        "permitted_change": "PARSER_CONTRACT_CORRECTION_ONLY",
        "operator_repair02_authorization_reused": False,
        "new_data_integrity_decision": False,
        "data_selection_change": "NONE",
        "quarantine_change": "NONE",
        "hypothesis_design_change": "NONE",
        "dependent_rfq_result_opened": False,
        "prerequisite_audit_path": mod.REPAIR03_AUDIT_ARCHIVE,
        "prerequisite_audit_sha256": audit_sha,
    }
    write_json(run_dir / mod.REPAIR03_AUTHORITY_BASIS, authority)
    authority_sha = mod.sha256(run_dir / mod.REPAIR03_AUTHORITY_BASIS)
    hashes = {
        "failed_state_sha256": mod.sha256(
            run_dir / mod.REPAIR03_FAILED_STATE_ARCHIVE
        ),
        "failed_resource_receipt_sha256": mod.sha256(
            run_dir / mod.REPAIR03_FAILED_RESOURCE_ARCHIVE
        ),
        "failed_scratch_receipt_sha256": mod.sha256(
            run_dir / mod.REPAIR03_FAILED_SCRATCH_ARCHIVE
        ),
        "failed_input_identity_sha256": mod.sha256(
            run_dir / mod.REPAIR03_FAILED_INPUT_ARCHIVE
        ),
        "inner_payload_audit_sha256": audit_sha,
        "inner_payload_audit_resource_sha256": mod.sha256(
            run_dir / mod.REPAIR03_AUDIT_RESOURCE_ARCHIVE
        ),
        "inner_payload_audit_source_sha256": audit_source_sha,
        "parser_contract_sha256": parser_sha,
        "authority_basis_sha256": authority_sha,
        "cycle1_binding_sha256": mod.sha256(
            run_dir / mod.REPAIR03_CYCLE1_BINDING_ARCHIVE
        ),
    }
    failed_attempt = {
        "attempt_id": "RFQ_FULL_STAGE_REPAIR02_ATTEMPT_03",
        "state_active_path": "REPORT/tables/RFQ_FULL_STAGE_STATE.json",
        "state_path": mod.REPAIR03_FAILED_STATE_ARCHIVE,
        "state_sha256": hashes["failed_state_sha256"],
        "state_status": "FAILED_DATA_INTEGRITY_REQUIRES_NEW_PREREGISTRATION",
        "resource_active_path": "logs/resources/rfq_full_stage_repair02.json",
        "resource_path": mod.REPAIR03_FAILED_RESOURCE_ARCHIVE,
        "resource_sha256": hashes["failed_resource_receipt_sha256"],
        "resource_label": mod.REPAIR02_SUCCESS_RESOURCE_LABEL,
        "return_code": 1,
        "scratch_receipt_active_path": (
            "DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT_03.json"
        ),
        "scratch_receipt_path": mod.REPAIR03_FAILED_SCRATCH_ARCHIVE,
        "scratch_receipt_sha256": hashes["failed_scratch_receipt_sha256"],
        "preserved_scratch_active_path": mod.REPAIR03_PRESERVED_SCRATCH,
        "preserved_scratch_sha256": preserved_sha,
        "preserved_scratch_bytes": preserved.stat().st_size,
        "input_identity_active_path": "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json",
        "input_identity_path": mod.REPAIR03_FAILED_INPUT_ARCHIVE,
        "input_identity_sha256": hashes["failed_input_identity_sha256"],
        "input_fingerprint": selection,
        "scratch_disposition": "PRESERVED_RENAMED_NO_RESUME",
        "retry_requirement": mod.REPAIR03_RETRY_REQUIREMENT,
    }
    coverage = {
        "status": "PARTIAL_OBJECT_COVERAGE_QUARANTINED",
        "full_unique_objects": 4,
        "full_logical_manifest_bindings": 4,
        "full_unique_bytes": 100,
        "retained_unique_objects": 2,
        "retained_logical_manifest_bindings": 2,
        "retained_bytes": 60,
        "quarantined_unique_objects": 2,
        "quarantined_logical_manifest_bindings": 2,
        "quarantined_bytes": 40,
        "full_object_set_sha256": full_set,
        "retained_object_set_sha256": retained_set,
        "quarantined_object_set_sha256": quarantined_set,
        "retained_selection_fingerprint_sha256": selection,
        "whole_object_quarantine": True,
        "line_salvage": False,
    }
    repair03 = {
        "schema_version": mod.REPAIR03_SCHEMA,
        "repair_id": "repair-03",
        "parent_repair_id": "repair-02",
        "applied_at_utc": applied_at,
        "pre_repair_status": (
            "CYCLE1_CORE_COMPLETE_RFQ_QUARANTINE_REPAIR02_REGISTERED"
        ),
        "post_repair_status": mod.REPAIR03_STATUS,
        "failed_stage_status": (
            "FAILED_DATA_INTEGRITY_REQUIRES_NEW_PREREGISTRATION"
        ),
        "rfq_result_state": "NO_RFQ_RESULT_OPENED",
        "retry_requirement": mod.REPAIR03_RETRY_REQUIREMENT,
        "finding": mod.REPAIR03_FINDING,
        "registration_change_class": mod.REPAIR03_CHANGE_CLASS,
        "source_verification_mode": mod.REPAIR03_SNAPSHOT_SOURCE_MODE,
        "quarantine_policy": (
            "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE_INHERITED_NO_CHANGE"
        ),
        "previous_repair_registration_path": parent_receipt,
        "previous_repair_registration_sha256": repair02[
            "repair_receipt_sha256"
        ],
        "previous_repair_record_sha256": mod._json_payload_sha256(repair02),
        "failed_state_path": mod.REPAIR03_FAILED_STATE_ARCHIVE,
        "failed_resource_receipt_path": mod.REPAIR03_FAILED_RESOURCE_ARCHIVE,
        "failed_scratch_receipt_path": mod.REPAIR03_FAILED_SCRATCH_ARCHIVE,
        "failed_input_identity_path": mod.REPAIR03_FAILED_INPUT_ARCHIVE,
        "inner_payload_audit_path": mod.REPAIR03_AUDIT_ARCHIVE,
        "inner_payload_audit_resource_path": mod.REPAIR03_AUDIT_RESOURCE_ARCHIVE,
        "inner_payload_audit_source_path": mod.REPAIR03_AUDIT_SOURCE,
        "inner_payload_audit_source_archive_path": (
            mod.REPAIR03_AUDIT_SOURCE_ARCHIVE
        ),
        "parser_contract_path": mod.REPAIR03_PARSER_CONTRACT,
        "authority_basis_path": mod.REPAIR03_AUTHORITY_BASIS,
        "cycle1_binding_path": mod.REPAIR03_CYCLE1_BINDING_ARCHIVE,
        **hashes,
        "registered_rfq_query_sha256": query_sha,
        "parser_contract": {
            "path": mod.REPAIR03_PARSER_CONTRACT,
            "sha256": parser_sha,
            "schema_version": mod.REPAIR03_PARSER_SCHEMA,
            "registered_query_sha256": query_sha,
            "audited_parser_contract_sha256": audited_sha,
            "audited_parser_contract": copy.deepcopy(
                mod.REPAIR03_AUDITED_PARSER_CONTRACT
            ),
        },
        "authority_basis": {
            "path": mod.REPAIR03_AUTHORITY_BASIS,
            "sha256": authority_sha,
            "schema_version": mod.REPAIR03_AUTHORITY_SCHEMA,
            "authority_basis": authority["authority_basis"],
            "permitted_change": authority["permitted_change"],
        },
        "newly_quarantined_objects": [],
        "cumulative_quarantined_objects": copy.deepcopy(cumulative),
        "coverage": coverage,
        "selection_identity_unchanged": True,
        "previous_selection_fingerprint_sha256": selection,
        "current_selection_fingerprint_sha256": selection,
        "failed_input_fingerprint": selection,
        "failed_attempt": failed_attempt,
        "expected_success_resource": {
            "label": mod.REPAIR03_SUCCESS_RESOURCE_LABEL,
            "path": mod.REPAIR03_SUCCESS_RESOURCE_PATH,
        },
        "inner_payload_audit": {
            "path": mod.REPAIR03_AUDIT_ARCHIVE,
            "sha256": audit_sha,
            "resource_path": mod.REPAIR03_AUDIT_RESOURCE_ARCHIVE,
            "resource_sha256": hashes["inner_payload_audit_resource_sha256"],
            "source_path": mod.REPAIR03_AUDIT_SOURCE,
            "source_sha256": audit_source_sha,
            "objects_scanned": 2,
            "bytes_scanned": 60,
            "lines_scanned": 3,
            "data_frame_rows": 1,
            "segment_receipt_rows": 1,
            "expected_blank_control_marker_rows": 1,
            "expected_blank_control_marker_counts": {"hour_open": 1},
            "outer_invalid_object_count": 0,
            "outer_invalid_line_count": 0,
            "identity_mismatch_count": 0,
            "unexpected_inner_payload_object_count": 0,
            "unexpected_inner_payload_row_count": 0,
            "audited_parser_contract_sha256": audited_sha,
            "analysis_result_opened": False,
        },
        "cycle1_duckdb_binding": cycle_binding,
        "previous_execution_commit": previous_commit,
        "current_execution_commit": current_commit,
        "initial_repository_identity": first_identity,
        "previous_repository_identity": previous_identity,
        "current_repository_identity": current_identity,
        "repository_identity_chain": repository["identity_history"],
        "previous_source_manifest_sha256": previous_identity[
            "source_manifest_sha256"
        ],
        "current_source_manifest_sha256": current_identity[
            "source_manifest_sha256"
        ],
        "previous_source_sha256s_sha256": previous_identity[
            "source_sha256s_sha256"
        ],
        "current_source_sha256s_sha256": current_identity[
            "source_sha256s_sha256"
        ],
        "previous_query_set_sha256": previous_identity["query_set_sha256"],
        "current_query_set_sha256": current_identity["query_set_sha256"],
        "core_result_disposition": "CORE_RESULTS_PRESERVED_NOT_RECOMPUTED",
        "core_results_recomputed": False,
        "core_result_artifacts": core_results,
        "hypothesis_design_change": "NONE",
        "data_selection_change": "NONE",
        "quarantine_change": "NONE",
        "threshold_feature_test_or_hypothesis_status_changed": False,
        "source_snapshot_attestation_active_path": (
            mod.REPAIR03_SOURCE_ATTESTATION
        ),
        "source_snapshot_attestation_path": (
            mod.REPAIR03_SOURCE_ATTESTATION_ARCHIVE
        ),
        "source_snapshot_attestation_sha256": mod.sha256(
            run_dir / mod.REPAIR03_SOURCE_ATTESTATION
        ),
        "source_snapshot_attestation": {
            "schema_version": mod.REPAIR03_SOURCE_ATTESTATION_SCHEMA,
            "execution_commit": current_commit,
            "git_tree": attestation["git_tree"],
            "direct_parent_verified": True,
            "source_relative": mod.REPAIR03_SOURCE_RELATIVE,
            "source_tree_clean_at_attestation": True,
            "commit_changed_paths": copy.deepcopy(mod.REPAIR03_CHANGED_PATHS),
        },
        "archive_path": mod.REPAIR03_PRE_ROOT,
    }
    repair03["archive_inventory"] = mod._archive_inventory(pre_root)

    first_suffix = {
        "trial_registration_id": "RFQ_FULL_STAGE_ATTEMPT_03",
        "result_opened": False,
        "hypothesis_conclusion_opened": False,
        "failed_state_sha256": hashes["failed_state_sha256"],
        "failed_resource_receipt_sha256": hashes[
            "failed_resource_receipt_sha256"
        ],
        "failed_scratch_receipt_sha256": hashes[
            "failed_scratch_receipt_sha256"
        ],
        "failed_input_identity_sha256": hashes[
            "failed_input_identity_sha256"
        ],
    }
    second_suffix = {
        "trial_registration_id": "RFQ_PARSER_CONTRACT_REPAIR_03",
        "result_opened": False,
        "hypothesis_conclusion_opened": False,
        "parser_contract_sha256": parser_sha,
        "current_selection_fingerprint_sha256": selection,
        "retry_requirement": mod.REPAIR03_RETRY_REQUIREMENT,
    }
    active_registry = pre_registry + b"".join(
        (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
        for row in (first_suffix, second_suffix)
    )
    (run_dir / "TRIAL_REGISTRY.jsonl").write_bytes(active_registry)
    (post_root / "TRIAL_REGISTRY.jsonl").write_bytes(active_registry)
    repair03["trial_registry"] = {
        "previous_sha256": hashlib.sha256(pre_registry).hexdigest(),
        "current_sha256": hashlib.sha256(active_registry).hexdigest(),
        "previous_bytes": len(pre_registry),
        "current_bytes": len(active_registry),
        "strict_previous_bytes_prefix": True,
        "appended_records": 2,
        "trial_registration_ids": [
            "RFQ_FULL_STAGE_ATTEMPT_03", "RFQ_PARSER_CONTRACT_REPAIR_03",
        ],
    }

    for placeholder in (
        mod.REPAIR03_REGISTRATION, mod.REPAIR03_TRANSACTION_JOURNAL,
    ):
        path = run_dir / placeholder
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{}\n")
    mutation_paths = [
        "SOURCE_MANIFEST.json", "SOURCE_SHA256SUMS.txt",
        "QUERY_SHA256SUMS.txt", query_relative, "TRIAL_REGISTRY.jsonl",
    ]
    mutations = [
        {
            "path": relative,
            "original_archive_path": f"{mod.REPAIR03_PRE_ROOT}/{relative}",
            "original_sha256": mod.sha256(pre_root / relative),
            "replacement_sha256": mod.sha256(run_dir / relative),
            "append_only_registry": relative == "TRIAL_REGISTRY.jsonl",
        }
        for relative in mutation_paths
    ]
    expected_repair_files = sorted(
        row["path"] for row in mod._archive_inventory(repair_root)
    )
    journal = {
        "schema_version": "repair03-registration-transaction-v1",
        "repair_id": "repair-03",
        "run_id": run_dir.name,
        "state": "PREPARED_BEFORE_ACTIVE_MUTATION",
        "original_manifest_sha256": mod.sha256(pre_root / "RUN_MANIFEST.json"),
        "active_mutations": mutations,
        "expected_repair_files": expected_repair_files,
    }
    write_json(run_dir / mod.REPAIR03_TRANSACTION_JOURNAL, journal)
    repair03["transaction_journal_path"] = mod.REPAIR03_TRANSACTION_JOURNAL
    repair03["transaction_journal_sha256"] = mod.sha256(
        run_dir / mod.REPAIR03_TRANSACTION_JOURNAL
    )
    receipt = copy.deepcopy(repair03)
    write_json(run_dir / mod.REPAIR03_REGISTRATION, receipt)
    repair03["repair_receipt_path"] = mod.REPAIR03_REGISTRATION
    repair03["repair_receipt_sha256"] = mod.sha256(
        run_dir / mod.REPAIR03_REGISTRATION
    )
    manifest = {
        "run_id": run_dir.name,
        "status": mod.REPAIR03_STATUS,
        "registration_state": mod.REPAIR03_REGISTRATION_STATE,
        "repository": repository,
        "data_integrity_repairs": [repair01, repair02, repair03],
    }
    write_json(run_dir / "RUN_MANIFEST.json", manifest)
    (run_dir / mod.QUARANTINE_DECLARATION_02).parent.mkdir(
        parents=True, exist_ok=True
    )
    (run_dir / mod.QUARANTINE_DECLARATION_02).write_bytes(b"{}\n")
    (run_dir / mod.MALFORMED_OBJECT_RECEIPT_02).write_bytes(b"{}\n")
    approved_fixture_values = {
        "REPAIR03_APPROVED_FAILED_STATE_SHA256": hashes["failed_state_sha256"],
        "REPAIR03_APPROVED_FAILED_RESOURCE_SHA256": hashes[
            "failed_resource_receipt_sha256"
        ],
        "REPAIR03_APPROVED_FAILED_INPUT_SHA256": hashes[
            "failed_input_identity_sha256"
        ],
        "REPAIR03_APPROVED_FAILED_SCRATCH_RECEIPT_SHA256": hashes[
            "failed_scratch_receipt_sha256"
        ],
        "REPAIR03_APPROVED_FAILED_SCRATCH_SHA256": preserved_sha,
        "REPAIR03_APPROVED_FAILED_SCRATCH_BYTES": preserved.stat().st_size,
        "REPAIR03_APPROVED_AUDIT_SHA256": audit_sha,
        "REPAIR03_APPROVED_AUDIT_RESOURCE_SHA256": hashes[
            "inner_payload_audit_resource_sha256"
        ],
        "REPAIR03_APPROVED_AUDIT_SOURCE_SHA256": audit_source_sha,
        "REPAIR03_APPROVED_AUDITED_PARSER_CONTRACT_SHA256": audited_sha,
        "REPAIR03_EXPECTED_RETAINED_OBJECTS": 2,
        "REPAIR03_EXPECTED_RETAINED_BYTES": 60,
        "REPAIR03_EXPECTED_LINES": 3,
        "REPAIR03_EXPECTED_BLANK_CONTROLS": 1,
        "REPAIR03_EXPECTED_DATA_FRAMES": 1,
        "REPAIR03_EXPECTED_SEGMENT_RECEIPTS": 1,
        "REPAIR03_EXPECTED_MARKER_COUNTS": marker_counts,
    }
    for name, value in approved_fixture_values.items():
        monkeypatch.setattr(mod, name, value)
    return {
        "run_dir": run_dir,
        "manifest": manifest,
        "inputs": inputs,
        "base_result": base_result,
        "repair03": repair03,
    }


def repair03_main_failure_fixture(tmp_path, monkeypatch):
    """Prepare main() immediately before its active-evidence transaction."""
    run_dir = tmp_path / "repair03-main-failure"
    (run_dir / "DATA_INTEGRITY").mkdir(parents=True)
    (run_dir / "REPORT/tables").mkdir(parents=True)
    (run_dir / "cache").mkdir(parents=True)
    active_input = run_dir / "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json"
    active_state = run_dir / "REPORT/tables/RFQ_FULL_STAGE_STATE.json"
    active_input.write_bytes(b'{"schema":"rfq-full-input-identity-v2"}\n')
    active_state.write_bytes(b'{"status":"FAILED_DATA_INTEGRITY_REQUIRES_NEW_PREREGISTRATION"}\n')
    manifest = {
        "run_id": run_dir.name,
        "data_integrity_repairs": [
            {"repair_id": "repair-01"},
            {"repair_id": "repair-02"},
            {"repair_id": "repair-03"},
        ],
    }
    object_row = {
        "key": "raw_rfq/date=2026-07-12/rfq_00.ndjson",
        "sha256": "a" * 64,
        "size": 1,
        "bound_release_ids": [mod.RELEASE_IDS[0]],
        "path": run_dir / "retained.ndjson",
    }
    inputs = {
        "releases": [],
        "paths": [object_row["path"]],
        "objects_detail": [object_row],
        "objects": 1,
        "logical_manifest_bindings": 1,
        "deduplicated_overlapping_objects": 0,
        "path_size_fingerprint_sha256": "b" * 64,
        "coverage_status": "PARTIAL_OBJECT_COVERAGE_QUARANTINED",
        "full_object_coverage": False,
        "unique_objects_total": 1,
        "unique_bytes_total": 1,
        "consumed_unique_objects": 1,
        "consumed_logical_bindings": 1,
        "consumed_bytes": 1,
        "consumed_object_set_sha256": "c" * 64,
        "quarantined_unique_objects": 2,
        "quarantined_logical_bindings": 2,
        "quarantined_bytes": 40,
        "quarantined_object_set_sha256": "d" * 64,
        "quarantine_reasons": ["whole-object quarantine"],
        "quarantine_details": [],
        "quarantine_boundaries": [],
        "selection_fingerprint_sha256": "e" * 64,
        "repair_chain": [{"repair_id": "repair-03"}],
        "failed_attempt_bindings": [{"repair_id": "repair-03"}],
        "expected_success_resource": {
            "label": mod.REPAIR03_SUCCESS_RESOURCE_LABEL,
            "path": mod.REPAIR03_SUCCESS_RESOURCE_PATH,
        },
        "inner_payload_parser_contract": {
            "path": mod.REPAIR03_PARSER_CONTRACT,
            "sha256": "f" * 64,
            "schema_version": mod.REPAIR03_PARSER_SCHEMA,
        },
        "registered_rfq_query_sha256": "1" * 64,
    }
    monkeypatch.setattr(mod, "validate_run", lambda _run: manifest)
    monkeypatch.setattr(mod, "discover_inputs", lambda _cache: {"unfrozen": True})
    monkeypatch.setattr(mod, "validate_input_bindings", lambda *_args: None)
    monkeypatch.setattr(
        mod, "apply_object_quarantine", lambda *_args: copy.deepcopy(inputs)
    )
    monkeypatch.setattr(
        mod,
        "validate_cycle1_duckdb_binding",
        lambda *_args: {"schema_version": "synthetic-cycle1-binding-v1"},
    )
    monkeypatch.setattr(mod, "EXPECTED_DUCKDB", duckdb.__version__)
    return {
        "run_dir": run_dir,
        "manifest": manifest,
        "inputs": inputs,
        "input_path": active_input,
        "state_path": active_state,
        "scratch": run_dir / "cache/rfq_full_scratch.duckdb",
        "input_before": active_input.read_bytes(),
        "state_before": active_state.read_bytes(),
    }


def quarantine_fixture(tmp_path):
    """Build a tiny exact-binding analogue using the known production failure key."""
    cache = tmp_path / "cache"
    run_dir = tmp_path / "test-run"
    run_dir.mkdir()
    base_ns = int(dt.datetime(
        2026, 7, 14, tzinfo=dt.timezone.utc
    ).timestamp() * 1_000_000_000)
    known_key = "raw_rfq/date=2026-07-14/rfq_00.ndjson"
    pre_key = "raw_rfq/date=2026-07-13/rfq_23.ndjson"
    next_key = known_key + ".1"
    receipt_key = "raw_rfq/date=2026-07-14/rfq_receipts_00.ndjson"
    other_key = "raw_rfq/date=2026-07-12/rfq_00.ndjson"
    bodies = {
        other_key: json.dumps(recorder(
            base_ns - 86_400_000_000_000, {"type": "subscribed", "sid": 7}
        )) + "\n",
        pre_key: "\n".join([
            json.dumps(recorder(base_ns + 1_000_000_000, frame(
                "rfq_created", "CROSS-GAP", "M-A", "2026-07-14T00:00:01Z"
            ))),
            json.dumps(recorder(
                base_ns + 2_000_000_000, {"type": "subscribed", "sid": 7}
            )),
        ]) + "\n",
        # Deliberately invalid outer NDJSON. A successful scan proves this path was not opened.
        known_key: '{"recv_wall_ns":123,"raw":"unterminated"\n',
        next_key: "\n".join([
            json.dumps(recorder(base_ns + 7_204_000_000_000, frame(
                "rfq_deleted", "CROSS-GAP", "M-A", "2026-07-14T02:00:04Z"
            ))),
            json.dumps(recorder(base_ns + 7_204_500_000_000, frame(
                "rfq_created", "POST-GAP", "M-B", "2026-07-14T02:00:04.5Z"
            ))),
            json.dumps(recorder(base_ns + 7_205_000_000_000, frame(
                "rfq_deleted", "POST-GAP", "M-B", "2026-07-14T02:00:05Z"
            ))),
        ]) + "\n",
        receipt_key: json.dumps(recorder(
            base_ns + 3_600_000_000_000,
            {"status": "PASS", "subscription_proven": True,
             "boundary_closed": True, "end_reason": "boundary", "findings": []},
            marker="segment_receipt",
        )) + "\n",
    }
    release_keys = {
        mod.RELEASE_IDS[0]: [other_key],
        mod.RELEASE_IDS[1]: [pre_key, known_key, next_key, receipt_key],
    }
    manifest_sha = {}
    version_paths = {}
    object_rows = {}
    for index, release_id in enumerate(mod.RELEASE_IDS):
        base = cache / "releases" / release_id
        rows = []
        versions = []
        for key in release_keys[release_id]:
            body = bodies[key]
            target = base / key
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
            row = {
                "key": key,
                "sha256": hashlib.sha256(body.encode()).hexdigest(),
                "size": len(body.encode()),
            }
            rows.append(row)
            object_rows[key] = row
            versions.append({**row, "version_id": f"fixture-version-{index}-{len(rows)}"})
        write_json(base / ".VERIFIED.json", {
            "release_id": release_id,
            "version_binding_mode": "VERSION_BOUND",
            "evidence_tier": mod.EVIDENCE,
        })
        write_json(base / "MANIFEST.json", {"objects": rows})
        manifest_sha[release_id] = mod.sha256(base / "MANIFEST.json")
        version_path = f"DATA_INTEGRITY/version_ids/release-{index}.jsonl"
        version_paths[release_id] = version_path
        path = run_dir / version_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in versions), encoding="utf-8")

    selected = [{
        "release_id": release_id,
        "manifest_sha256": manifest_sha[release_id],
        "exact_version_list_path": version_paths[release_id],
    } for release_id in mod.RELEASE_IDS]
    execution_commit = "1" * 40
    manifest = {
        "selected_releases": selected,
        "repository": {
            "execution_commit": execution_commit,
            "source_manifest_sha256": "2" * 64,
            "query_set_sha256": "3" * 64,
        },
    }
    failed = object_rows[known_key]
    version_id = "fixture-version-1-2"
    receipt = {
        "schema_version": "rfq-malformed-object-receipt-v1",
        "run_id": run_dir.name,
        "release_id": mod.RELEASE_IDS[1],
        "key": known_key,
        "expected_size": failed["size"],
        "observed_size": failed["size"],
        "expected_sha256": failed["sha256"],
        "observed_sha256": failed["sha256"],
        "invalid_line_count": 1,
        "invalid_lines": [{
            "line_number": 1,
            "line_sha256": hashlib.sha256(bodies[known_key].encode()).hexdigest(),
            "error_type": "JSONDecodeError",
        }],
        "total_lines": 1,
        "raw_payload_redacted": True,
        "disposition": "CHANNEL_OBJECT_QUARANTINE_REQUIRED",
    }
    write_json(run_dir / mod.MALFORMED_OBJECT_RECEIPT, receipt)
    declaration = {
        "schema_version": "rfq-object-quarantine-v1",
        "run_id": run_dir.name,
        "mode": "EXPLORATORY_AUTORESEARCH",
        "disposition": "WHOLE_OBJECT_QUARANTINE",
        "finding": "MALFORMED_NDJSON_OBJECT",
        "created_after_structural_failure_before_rfq_result": True,
        "dependent_rfq_result_opened": False,
        "remaining_object_parse_policy": mod.STRICT_REMAINING_PARSE_POLICY,
        "authority_basis": "Mission section 22 isolated-channel quarantine.",
        "selection_rule": "Exact manifest, VersionId, and receipt binding; whole object only.",
        "result_use_prohibited": "No row or result from the quarantined object.",
        "source_execution_commit": execution_commit,
        "quarantined_objects": [{
            "release_id": mod.RELEASE_IDS[1],
            "key": known_key,
            "size": failed["size"],
            "sha256": failed["sha256"],
            "version_id": version_id,
            "manifest_sha256": manifest_sha[mod.RELEASE_IDS[1]],
            "invalid_line_count": 1,
            "reason": "Strict parsing proved malformed bytes; whole object excluded.",
            "receipt": mod.MALFORMED_OBJECT_RECEIPT,
        }],
    }
    write_json(run_dir / mod.QUARANTINE_DECLARATION, declaration)
    return {
        "cache": cache,
        "run_dir": run_dir,
        "manifest": manifest,
        "declaration": declaration,
        "receipt": receipt,
        "known_key": known_key,
        "known_path": cache / "releases" / mod.RELEASE_IDS[1] / known_key,
        "next_path": cache / "releases" / mod.RELEASE_IDS[1] / next_key,
        "receipt_key": receipt_key,
        "receipt_path": cache / "releases" / mod.RELEASE_IDS[1] / receipt_key,
        "base_ns": base_ns,
    }


def synthetic_cycle1_binding(run_id="synthetic-run"):
    return {
        "active_path": mod.CYCLE1_DUCKDB_BINDING,
        "active_sha256": "1" * 64,
        "archived_path": mod.CYCLE1_DUCKDB_BINDING_ARCHIVE,
        "archived_sha256": "1" * 64,
        "schema_version": "cycle1-derived-duckdb-binding-v1",
        "run_id": run_id,
        "duckdb_path": f"/srv/w09-research/runs/{run_id}/cache/cycle1.duckdb",
        "duckdb_bytes": 4096,
        "duckdb_sha256": "2" * 64,
        "core_stage_resource_path": mod.CYCLE1_CORE_RESOURCE,
        "core_stage_resource_sha256": "3" * 64,
        "core_summary_path": mod.CYCLE1_CORE_SUMMARY,
        "core_summary_sha256": "4" * 64,
    }


def cycle1_binding_fixture(tmp_path):
    run_dir = tmp_path / "binding-run"
    database = run_dir / "cache/cycle1.duckdb"
    database.parent.mkdir(parents=True)
    connection = duckdb.connect(str(database))
    connection.execute("CREATE TABLE preserved_core(value INTEGER)")
    connection.execute("INSERT INTO preserved_core VALUES (1)")
    connection.close()

    summary_path = run_dir / mod.CYCLE1_CORE_SUMMARY
    write_json(summary_path, {
        "run_id": run_dir.name,
        "banner": mod.BANNER,
        "boundary": "No result is promotion ready.",
    })
    resource_path = run_dir / mod.CYCLE1_CORE_RESOURCE
    write_json(resource_path, {
        "schema_version": "w09-stage-resource-v1",
        "label": "cycle1_core",
        "return_code": 0,
        "command": ["python", "run_cycle1.py", "--run-dir", str(run_dir)],
    })
    receipt = {
        "schema_version": "cycle1-derived-duckdb-binding-v1",
        "run_id": run_dir.name,
        "path": str(database.resolve()),
        "bytes": database.stat().st_size,
        "sha256": mod.sha256(database),
        "mtime_utc": dt.datetime.fromtimestamp(
            database.stat().st_mtime, tz=dt.timezone.utc
        ).isoformat(),
        "duckdb_version": mod.EXPECTED_DUCKDB,
        "created_before_rfq_repair_registration": True,
        "core_result_disposition": (
            "CORE_DERIVED_DATABASE_PRESERVED_NOT_RECOMPUTED"
        ),
        "core_summary_path": mod.CYCLE1_CORE_SUMMARY,
        "core_summary_sha256": mod.sha256(summary_path),
        "core_stage_resource_path": mod.CYCLE1_CORE_RESOURCE,
        "core_stage_resource_sha256": mod.sha256(resource_path),
    }
    active = run_dir / mod.CYCLE1_DUCKDB_BINDING
    archived = run_dir / mod.CYCLE1_DUCKDB_BINDING_ARCHIVE
    write_json(active, receipt)
    write_json(archived, receipt)
    binding = {
        "active_path": mod.CYCLE1_DUCKDB_BINDING,
        "active_sha256": mod.sha256(active),
        "archived_path": mod.CYCLE1_DUCKDB_BINDING_ARCHIVE,
        "archived_sha256": mod.sha256(archived),
        "schema_version": receipt["schema_version"],
        "run_id": receipt["run_id"],
        "duckdb_path": receipt["path"],
        "duckdb_bytes": receipt["bytes"],
        "duckdb_sha256": receipt["sha256"],
        "core_stage_resource_path": receipt["core_stage_resource_path"],
        "core_stage_resource_sha256": receipt["core_stage_resource_sha256"],
        "core_summary_path": receipt["core_summary_path"],
        "core_summary_sha256": receipt["core_summary_sha256"],
    }
    manifest = {
        "data_integrity_repairs": [{
            "repair_id": "repair-01",
            "finding": "MALFORMED_NDJSON_OBJECT",
            "cycle1_duckdb_binding": binding,
        }],
    }
    return {
        "run_dir": run_dir,
        "database": database,
        "active": active,
        "archived": archived,
        "receipt": receipt,
        "binding": binding,
        "manifest": manifest,
    }


def double_quarantine_contract_fixture(tmp_path):
    run_dir = tmp_path / "double-contract-run"
    run_dir.mkdir()
    release_id = mod.RELEASE_IDS[1]
    manifest_sha = "9" * 64
    rows = []
    specs = [
        ("raw_rfq/date=2026-07-13/rfq_23.ndjson.1", 101, "1" * 64),
        ("raw_rfq/date=2026-07-13/rfq_23.ndjson.2", 102, "2" * 64),
        ("raw_rfq/date=2026-07-14/rfq_00.ndjson", 103, "3" * 64),
        ("raw_rfq/date=2026-07-14/rfq_00.ndjson.1", 104, "4" * 64),
    ]
    for key, size, digest in specs:
        rows.append({
            "key": key, "size": size, "sha256": digest,
            "path": tmp_path / "cache" / release_id / key,
            "bound_release_ids": [release_id],
        })
    full_fp = mod._object_fingerprint(rows)
    inputs = {
        "release_ids": list(mod.RELEASE_IDS),
        "releases": [{"release_id": release_id, "manifest_sha256": manifest_sha}],
        "paths": [row["path"] for row in rows],
        "objects_detail": rows,
        "objects": 4,
        "logical_manifest_bindings": 4,
        "deduplicated_overlapping_objects": 0,
        "overlap_keys": [],
        "bytes": sum(row["size"] for row in rows),
        "path_size_fingerprint_sha256": full_fp,
    }
    version_path = "DATA_INTEGRITY/version_ids/double.jsonl"
    version_file = run_dir / version_path
    version_file.parent.mkdir(parents=True)
    version_rows = []
    for index, row in enumerate(rows):
        version_rows.append({
            "key": row["key"], "size": row["size"], "sha256": row["sha256"],
            "version_id": f"version-{index}",
        })
    version_file.write_text(
        "".join(json.dumps(row) + "\n" for row in version_rows), encoding="utf-8"
    )
    first_object = {
        "release_id": release_id,
        "key": rows[2]["key"], "size": rows[2]["size"],
        "sha256": rows[2]["sha256"], "version_id": "version-2",
        "manifest_sha256": manifest_sha, "invalid_line_count": 1,
        "reason": "repair-01 no line-level salvage",
        "receipt": mod.MALFORMED_OBJECT_RECEIPT,
    }
    second_object = {
        "release_id": release_id,
        "key": rows[1]["key"], "size": rows[1]["size"],
        "sha256": rows[1]["sha256"], "version_id": "version-1",
        "manifest_sha256": manifest_sha, "invalid_line_count": 1,
        "reason": "repair-02 no line-level salvage",
        "receipt": mod.MALFORMED_OBJECT_RECEIPT_02,
    }
    cumulative = [second_object, first_object]
    old_commit, new_commit = "a" * 40, "b" * 40
    auth = {
        "schema_version": "sports-autoresearch-repair-authorization-v1",
        "run_id": run_dir.name,
        "repair_id": "repair-02",
        "authorized_at_utc": "2026-07-15T14:40:39Z",
        "authority_source": "operator_chat_message",
        "authorized_action": mod.REPAIR02_AUTHORIZED_ACTION,
    }
    auth_path = run_dir / "DATA_INTEGRITY/REPAIR_02_USER_AUTHORIZATION.json"
    write_json(auth_path, auth)
    declaration = {
        "schema_version": "rfq-object-quarantine-v2", "run_id": run_dir.name,
        "mode": "EXPLORATORY_AUTORESEARCH",
        "finding": "SECOND_DISTINCT_MANIFEST_BOUND_MALFORMED_RFQ_OBJECT",
        "disposition": "WHOLE_OBJECT_QUARANTINE",
        "authority_basis": "EXPLICIT_OPERATOR_AUTHORIZATION_REPAIR_02",
        "created_at_utc": "2026-07-15T14:42:31Z",
        "created_after_structural_failure_before_rfq_result": True,
        "dependent_rfq_result_opened": False,
        "source_execution_commit": old_commit,
        "parent_repair_id": "repair-01",
        "previous_declaration_path": mod.REPAIR_DECLARATION_ARCHIVE,
        "previous_declaration_sha256": "5" * 64,
        "authorization_evidence_path": "DATA_INTEGRITY/REPAIR_02_USER_AUTHORIZATION.json",
        "authorization_evidence_sha256": mod.sha256(auth_path),
        "newly_quarantined_objects": [second_object],
        "cumulative_quarantined_objects": cumulative,
        "remaining_object_parse_policy": mod.STRICT_REMAINING_PARSE_POLICY,
        "selection_rule": "Exclude exactly two objects.",
        "result_use_prohibited": "No result; no line-level salvage is permitted.",
        "trial_disposition_if_repair_fails": (
            "ABORT_WITHOUT_RFQ_RESULT; new preregistration required."
        ),
    }
    declaration_path = run_dir / mod.QUARANTINE_DECLARATION_02
    write_json(declaration_path, declaration)
    receipt = {
        "schema_version": "rfq-malformed-object-receipt-v1",
        "run_id": run_dir.name, "release_id": release_id,
        "key": second_object["key"], "expected_size": second_object["size"],
        "observed_size": second_object["size"],
        "expected_sha256": second_object["sha256"],
        "observed_sha256": second_object["sha256"],
        "manifest_sha256": manifest_sha, "version_id": "version-1",
        "invalid_line_count": 1,
        "invalid_lines": [{
            "line_number": 1, "line_sha256": "6" * 64,
            "error_type": "JSONDecodeError",
        }],
        "total_lines": 1, "raw_payload_redacted": True,
        "final_line_newline_terminated": True,
        "quarantine_authorized": False,
        "disposition": "CHANNEL_OBJECT_QUARANTINE_REQUIRED",
    }
    receipt_path = run_dir / mod.MALFORMED_OBJECT_RECEIPT_02
    write_json(receipt_path, receipt)
    repair01 = {
        "schema_version": "sports-autoresearch-data-integrity-repair-v1",
        "repair_id": "repair-01", "current_execution_commit": old_commit,
        "current_source_manifest_sha256": "7" * 64,
        "current_query_set_sha256": "8" * 64,
        "declaration_sha256": "5" * 64,
        "receipt_sha256": "4" * 64,
        "quarantined_objects": [first_object],
    }
    retained = [rows[0], rows[3]]
    quarantined = [rows[1], rows[2]]
    selection = mod._repair02_selection_fingerprint(
        full_fp, retained, quarantined, mod.sha256(declaration_path),
        [repair01["receipt_sha256"], mod.sha256(receipt_path)],
    )
    coverage = {
        "status": "PARTIAL_OBJECT_COVERAGE_QUARANTINED",
        "full_unique_objects": 4, "full_logical_manifest_bindings": 4,
        "full_unique_bytes": inputs["bytes"],
        "retained_unique_objects": 2, "retained_logical_manifest_bindings": 2,
        "retained_bytes": sum(row["size"] for row in retained),
        "quarantined_unique_objects": 2,
        "quarantined_logical_manifest_bindings": 2,
        "quarantined_bytes": sum(row["size"] for row in quarantined),
        "full_object_set_sha256": full_fp,
        "retained_object_set_sha256": mod._object_fingerprint(retained),
        "quarantined_object_set_sha256": mod._object_fingerprint(quarantined),
        "retained_selection_fingerprint_sha256": selection,
        "whole_object_quarantine": True, "line_salvage": False,
    }
    repair02 = {
        "schema_version": "sports-autoresearch-data-integrity-repair-v2",
        "repair_id": "repair-02", "parent_repair_id": "repair-01",
        "finding": "SECOND_DISTINCT_MANIFEST_BOUND_MALFORMED_RFQ_OBJECT",
        "pre_repair_status": "CYCLE1_CORE_COMPLETE_RFQ_QUARANTINE_REPAIR_REGISTERED",
        "post_repair_status": "CYCLE1_CORE_COMPLETE_RFQ_QUARANTINE_REPAIR02_REGISTERED",
        "previous_execution_commit": old_commit,
        "current_execution_commit": new_commit,
        "previous_source_manifest_sha256": "7" * 64,
        "current_source_manifest_sha256": "c" * 64,
        "previous_query_set_sha256": "8" * 64,
        "current_query_set_sha256": "d" * 64,
        "newly_quarantined_objects": [second_object],
        "cumulative_quarantined_objects": cumulative,
        "coverage": coverage,
    }
    manifest = {
        "status": "CYCLE1_CORE_COMPLETE_RFQ_QUARANTINE_REPAIR02_REGISTERED",
        "selected_releases": [{
            "release_id": release_id, "manifest_sha256": manifest_sha,
            "exact_version_list_path": version_path,
        }],
        "repository": {
            "execution_commit": new_commit, "source_manifest_sha256": "c" * 64,
            "query_set_sha256": "d" * 64,
        },
        "data_integrity_repairs": [repair01, repair02],
    }
    first = {
        "quarantined_unique_objects": 1,
        "quarantine_details": [{
            **{key: first_object[key] for key in (
                "release_id", "key", "size", "sha256", "version_id",
                "manifest_sha256", "invalid_line_count", "reason",
            )},
            "receipt_sha256": repair01["receipt_sha256"],
            "declaration_sha256": repair01["declaration_sha256"],
        }],
        "failed_attempt_binding": {"repair_id": "repair-01"},
        "selection_fingerprint_sha256": "e" * 64,
    }
    return {
        "run_dir": run_dir, "inputs": inputs, "manifest": manifest,
        "first": first, "selection": selection, "second": second_object,
    }


def test_fixed_exact_rejects_rounding_and_overflow():
    assert mod.fixed_exact("1.25", 2) == 125
    assert mod.fixed_exact(10, 6) == 10_000_000
    assert mod.fixed_exact("1.234", 2) is None
    assert mod.fixed_exact(float("inf"), 2) is None
    assert mod.fixed_exact(True, 2) is None
    assert mod.fixed_exact("100000000000000000000", 2) is None


def test_requester_hash_is_scoped_and_empty_stays_missing():
    assert mod.requester_hash("abc", "r1") == mod.requester_hash("abc", "r1")
    assert mod.requester_hash("abc", "r1") != mod.requester_hash("abc", "r2")
    assert mod.requester_hash("", "r1") is None
    assert "abc" not in mod.requester_hash("abc", "r1")


def test_kaplan_meier_grouped_censoring():
    rows = mod.kaplan_meier_from_counts([(1, 1, 0), (2, 1, 1), (3, 1, 0)])
    assert rows[0] == (1, 4, 1, 0, 0.75)
    assert rows[1][1:4] == (3, 1, 1)
    assert rows[-1][-1] == 0.0


def test_windows_are_contiguous_and_causal_boundaries():
    mod.validate_windows()
    assert mod.WINDOWS_US[0][1] == -30_000_000
    assert mod.WINDOWS_US[-1][2] == 120_000_000
    for left, right in zip(mod.WINDOWS_US, mod.WINDOWS_US[1:]):
        assert left[2] == right[1]


def test_cycle1_duckdb_binding_validates_and_returns_exact_nested_identity(tmp_path):
    fixture = cycle1_binding_fixture(tmp_path)
    observed = mod.validate_cycle1_duckdb_binding(
        fixture["run_dir"], fixture["manifest"], fixture["database"]
    )
    assert observed == fixture["binding"]


def test_cycle1_duckdb_binding_rejects_database_tamper(tmp_path):
    fixture = cycle1_binding_fixture(tmp_path)
    with fixture["database"].open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(mod.RFQStageError, match="byte count|SHA-256"):
        mod.validate_cycle1_duckdb_binding(
            fixture["run_dir"], fixture["manifest"], fixture["database"]
        )


def test_cycle1_duckdb_binding_rejects_same_size_database_tamper(tmp_path):
    fixture = cycle1_binding_fixture(tmp_path)
    with fixture["database"].open("r+b") as handle:
        handle.seek(-1, 2)
        original = handle.read(1)
        handle.seek(-1, 2)
        handle.write(bytes([original[0] ^ 0xFF]))
    assert fixture["database"].stat().st_size == fixture["receipt"]["bytes"]
    with pytest.raises(mod.RFQStageError, match="SHA-256"):
        mod.validate_cycle1_duckdb_binding(
            fixture["run_dir"], fixture["manifest"], fixture["database"]
        )


def test_cycle1_duckdb_binding_rejects_receipt_or_manifest_tamper(tmp_path):
    fixture = cycle1_binding_fixture(tmp_path)
    active_receipt = json.loads(fixture["active"].read_text(encoding="utf-8"))
    active_receipt["core_summary_sha256"] = "f" * 64
    write_json(fixture["active"], active_receipt)
    with pytest.raises(mod.RFQStageError, match="active/archived"):
        mod.validate_cycle1_duckdb_binding(
            fixture["run_dir"], fixture["manifest"], fixture["database"]
        )


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("core_summary_sha256", "core summary SHA"),
        ("core_stage_resource_sha256", "core resource receipt SHA"),
    ],
)
def test_cycle1_binding_rehashes_core_artifacts_even_if_receipts_are_reforged(
    tmp_path, field, message
):
    fixture = cycle1_binding_fixture(tmp_path)
    receipt = json.loads(fixture["active"].read_text(encoding="utf-8"))
    receipt[field] = "f" * 64
    write_json(fixture["active"], receipt)
    write_json(fixture["archived"], receipt)
    forged_binding = dict(fixture["binding"])
    forged_binding.update({
        "active_sha256": mod.sha256(fixture["active"]),
        "archived_sha256": mod.sha256(fixture["archived"]),
        field: receipt[field],
    })
    fixture["manifest"]["data_integrity_repairs"][0][
        "cycle1_duckdb_binding"
    ] = forged_binding
    with pytest.raises(mod.RFQStageError, match=message):
        mod.validate_cycle1_duckdb_binding(
            fixture["run_dir"], fixture["manifest"], fixture["database"]
        )

    fixture = cycle1_binding_fixture(tmp_path / "manifest")
    fixture["manifest"]["data_integrity_repairs"][0][
        "cycle1_duckdb_binding"
    ]["duckdb_sha256"] = "f" * 64
    with pytest.raises(mod.RFQStageError, match="repair Cycle-1"):
        mod.validate_cycle1_duckdb_binding(
            fixture["run_dir"], fixture["manifest"], fixture["database"]
        )


@pytest.mark.parametrize("run_id", [None, "wrong-run"])
def test_cycle1_duckdb_binding_rejects_missing_or_mismatched_run_id(
    tmp_path, run_id
):
    fixture = cycle1_binding_fixture(tmp_path)
    receipt = json.loads(fixture["active"].read_text(encoding="utf-8"))
    if run_id is None:
        receipt.pop("run_id")
    else:
        receipt["run_id"] = run_id
    write_json(fixture["active"], receipt)
    write_json(fixture["archived"], receipt)
    with pytest.raises(mod.RFQStageError, match="field set|identity mismatch"):
        mod.validate_cycle1_duckdb_binding(
            fixture["run_dir"], fixture["manifest"], fixture["database"]
        )


def test_rfq_summary_rejects_absent_run_id_before_querying():
    with pytest.raises(mod.RFQStageError, match="summary run_id is missing"):
        mod.build_summary(
            None,
            {"cycle1_duckdb_binding": synthetic_cycle1_binding()},
            {},
            0.0,
        )


def test_schema_first_dedupe_lifecycle_legs_and_receive_clock(tmp_path):
    release0 = mod.RELEASE_IDS[0]
    base = tmp_path / "releases" / release0 / "raw_rfq" / "date=2026-07-12"
    rows = [
        # Delete before any create remains unmatched.
        recorder(500_000_000, frame("rfq_deleted", "B", "M-B", "2026-07-12T00:00:00Z")),
        recorder(1_000_000_000, frame(
            "rfq_created", "A", "M-A", "2026-07-12T00:00:10Z",
            contracts_fp="1.25", target_cost_dollars="10.000000",
            mve_collection_ticker="COLL", mve_selected_legs=[
                {"event_ticker": "E", "market_ticker": "M-A", "side": "YES",
                 "yes_settlement_value_dollars": "1.000000"}
            ],
        )),
        # Same exchange identity is a recorder duplicate even with later receive.
        recorder(1_100_000_000, frame(
            "rfq_created", "A", "M-A", "2026-07-12T00:00:10Z",
            contracts_fp="1.25", target_cost_dollars="10.000000",
            mve_collection_ticker="COLL", mve_selected_legs=[
                {"event_ticker": "E", "market_ticker": "M-A", "side": "YES",
                 "yes_settlement_value_dollars": "1.000000"}
            ],
        )),
        # Inconsistent market is not a valid lifecycle endpoint.
        recorder(1_500_000_000, frame(
            "rfq_deleted", "A", "M-X", "2026-07-12T00:00:11Z", creator_id="secret-id"
        )),
        # Exchange timestamp goes backwards, but TL1 receive ordering is authoritative.
        recorder(2_000_000_000, frame(
            "rfq_deleted", "A", "M-A", "2026-07-12T00:00:09Z", creator_id="secret-id"
        )),
        # Re-used ID starts a second cycle and is censored.
        recorder(3_000_000_000, frame(
            "rfq_created", "A", "M-A", "2026-07-12T00:00:12Z", contracts_fp="2.00"
        )),
        recorder(4_000_000_000, frame(
            "rfq_created", "C", "M-C", "2026-07-12T00:00:13Z", contracts_fp="1.234"
        )),  # invalid fixed point; schema-audited but excluded
        recorder(5_000_000_000, {"type": "subscribed", "msg": {"channel": "communications", "sid": 7}}),
    ]
    source = base / "rfq_00.ndjson"
    write_rows(source, rows)

    connection = duckdb.connect()
    mod.build_scan_tables(connection, [source], "test-run")
    mod.build_request_tables(connection, "test-run")

    counts = connection.execute(
        "SELECT recorder_rows,deduplicated_valid_frames FROM rfq_scan_counts"
    ).fetchone()
    assert counts == (len(rows), 5)
    requests = connection.execute(
        "SELECT cycle_no,delete_recv_ns,requester_hash,inconsistent_delete_rows "
        "FROM rfq_requests_base ORDER BY cycle_no"
    ).fetchall()
    assert len(requests) == 2
    assert requests[0][0] == 1 and requests[0][1] == 2_000_000_000
    assert requests[0][2] == mod.requester_hash("secret-id", "test-run")
    assert requests[0][3] == 1
    assert requests[1][0] == 2 and requests[1][1] is None
    lifecycle = connection.execute(
        "SELECT cycle_no,duration_us,delete_observed,exchange_order_anomaly "
        "FROM rfq_lifecycle_base ORDER BY cycle_no"
    ).fetchall()
    assert lifecycle[0] == (1, 1_000_000, True, True)
    assert lifecycle[1][2] is False
    legs = connection.execute(
        "SELECT leg_index,market_ticker,side,yes_settlement_value_e6,leg_schema_valid "
        "FROM rfq_legs_base"
    ).fetchall()
    assert legs == [(0, "M-A", "yes", 1_000_000, True)]
    # Raw requester identifiers must not be present in persistent output schemas/values.
    columns = [row[1] for row in connection.execute("PRAGMA table_info('rfq_requests_base')").fetchall()]
    assert "creator_id" not in columns and "delete_creator_id" not in columns
    assert not any("secret-id" in str(value) for row in requests for value in row)
    audit_fields = {
        row[0] for row in connection.execute(
            "SELECT field_name FROM rfq_schema_field_audit WHERE event_type='rfq_created'"
        ).fetchall()
    }
    assert {"id", "market_ticker", "contracts_fp", "mve_selected_legs"} <= audit_fields
    connection.close()


def test_nullable_requester_and_observation_boundary_censoring(tmp_path):
    source = (
        tmp_path / "releases" / mod.RELEASE_IDS[0] / "raw_rfq" /
        "date=2026-07-12/rfq_00.ndjson"
    )
    delete_without_requester = frame(
        "rfq_deleted", "X", "M-X", "2026-07-12T00:00:03Z"
    )
    delete_without_requester["msg"].pop("creator_id")
    write_rows(source, [
        recorder(1_000_000_000, frame(
            "rfq_created", "X", "M-X", "2026-07-12T00:00:01Z", creator_id=None
        )),
        recorder(2_000_000_000, marker="transport_close"),
        recorder(3_000_000_000, delete_without_requester),
        # A present non-string requester is a schema error; missing/JSON null is not.
        recorder(4_000_000_000, frame(
            "rfq_created", "Y", "M-Y", "2026-07-12T00:00:04Z", creator_id=123
        )),
        recorder(5_000_000_000, {"type": "subscribed", "sid": 7}),
    ])
    connection = duckdb.connect()
    mod.build_scan_tables(connection, [source], "boundary-run")
    mod.build_request_tables(connection, "boundary-run")

    assert connection.execute(
        "SELECT valid_contract_frames,invalid_contract_frames FROM rfq_scan_counts"
    ).fetchone() == (2, 1)
    request = connection.execute("""
      SELECT matched_delete_recv_ns,delete_recv_ns,requester_known,
        delete_crosses_observation_boundary
      FROM rfq_requests_base
    """).fetchone()
    assert request == (3_000_000_000, None, False, True)
    lifecycle = connection.execute("""
      SELECT endpoint_receive_us,duration_us,delete_observed,endpoint_type,
        observation_boundary_reason
      FROM rfq_lifecycle_base
    """).fetchone()
    assert lifecycle == (
        2_000_000, 1_000_000, False,
        "RIGHT_CENSORED_AT_OBSERVATION_BOUNDARY", "MARKER_TRANSPORT_CLOSE",
    )
    assert connection.execute("""
      SELECT count(*) FROM rfq_unmatched_deletes
      WHERE disposition='CROSS_OBSERVATION_BOUNDARY_DELETE'
    """).fetchone()[0] == 1
    connection.close()


def test_inner_payload_contract_accepts_exact_blank_controls_and_json_objects(
    tmp_path,
):
    source = (
        tmp_path / "releases" / mod.RELEASE_IDS[0] / "raw_rfq" /
        "date=2026-07-12/rfq_00.ndjson"
    )
    base_ns = int(dt.datetime(
        2026, 7, 12, tzinfo=dt.timezone.utc
    ).timestamp() * 1_000_000_000)
    receipt = {
        "status": "PASS",
        "subscription_proven": True,
        "boundary_closed": True,
        "end_reason": "boundary",
        "findings": [],
    }
    rows = [
        recorder(base_ns + 1, {"type": "subscribed", "sid": 7}),
        recorder(base_ns + 2, receipt, marker="segment_receipt"),
    ]
    rows.extend(
        recorder(base_ns + 3 + index, marker=marker)
        for index, marker in enumerate(mod.EXPECTED_BLANK_CONTROL_MARKERS)
    )
    write_rows(source, rows)

    connection = duckdb.connect()
    connection.execute(mod.scan_sql([source], "inner-contract-run"))
    flags = connection.execute("""
      SELECT coalesce(marker,'<frame>'),inner_json_valid,
        expected_blank_control_marker,inner_payload_contract_valid
      FROM rfq_scan_rows ORDER BY recv_wall_ns
    """).fetchall()
    assert flags[:2] == [
        ("<frame>", True, False, True),
        ("segment_receipt", True, False, True),
    ]
    assert flags[2:] == [
        (marker, False, True, True)
        for marker in mod.EXPECTED_BLANK_CONTROL_MARKERS
    ]

    # Continue through normalization to prove the accepted marker rows remain
    # available to channel QC and lifecycle-boundary construction.
    mod.build_scan_tables(connection, [source], "inner-contract-run")
    assert connection.execute("""
      SELECT recorder_rows,invalid_inner_json_rows,
        invalid_inner_payload_contract_rows,expected_blank_control_marker_rows,
        marker_rows,loss_gap_markers,receipt_rows,healthy_receipts
      FROM rfq_channel_qc
    """).fetchone() == (7, 0, 0, 5, 6, 4, 1, 1)
    assert connection.execute("""
      SELECT reason FROM rfq_observation_boundaries ORDER BY boundary_ns
    """).fetchall() == [
        ("MARKER_GAP",),
        ("MARKER_LOSS",),
        ("MARKER_TRANSPORT_CLOSE",),
        ("MARKER_TRANSPORT_ERROR",),
    ]
    connection.close()


def test_mutation_guard_next_create_censors_prior_and_delete_stays_new_cycle(tmp_path):
    source = (
        tmp_path / "releases" / mod.RELEASE_IDS[0] / "raw_rfq" /
        "date=2026-07-12/rfq_00.ndjson"
    )
    write_rows(source, [
        recorder(1_000_000_000, frame(
            "rfq_created", "REUSED", "M-A", "2026-07-12T00:00:01Z"
        )),
        recorder(3_000_000_000, frame(
            "rfq_created", "REUSED", "M-A", "2026-07-12T00:00:03Z"
        )),
        # This delete belongs only to cycle 2 and must never backfill cycle 1.
        recorder(4_000_000_000, frame(
            "rfq_deleted", "REUSED", "M-A", "2026-07-12T00:00:04Z"
        )),
        recorder(5_000_000_000, {"type": "subscribed", "sid": 7}),
    ])
    connection = duckdb.connect()
    mod.build_scan_tables(connection, [source], "replacement-run")
    mod.build_request_tables(connection, "replacement-run")

    lifecycle = connection.execute("""
      SELECT cycle_no,endpoint_receive_us,duration_us,delete_observed,endpoint_type,
        next_create_receive_us
      FROM rfq_lifecycle_base ORDER BY cycle_no
    """).fetchall()
    assert lifecycle == [
        (1, 3_000_000, 2_000_000, False, "RIGHT_CENSORED_AT_NEXT_CREATE", 3_000_000),
        (2, 4_000_000, 1_000_000, True, "FIRST_VALID_DELETE", None),
    ]
    requests = connection.execute("""
      SELECT cycle_no,delete_recv_ns,censor_boundary_type
      FROM rfq_requests_base ORDER BY cycle_no
    """).fetchall()
    assert requests == [
        (1, None, "NEXT_CREATE_REPLACEMENT"),
        (2, 4_000_000_000, None),
    ]
    assert connection.execute("""
      SELECT count(*) FROM rfq_unmatched_deletes WHERE disposition='MATCHED_ENDPOINT'
    """).fetchone()[0] == 1
    connection.close()


def test_manifest_object_key_sha_overlap_is_deduplicated_and_conflict_fails(tmp_path):
    key = "raw_rfq/date=2026-07-13/rfq_00.ndjson"
    body = json.dumps(recorder(1_000_000_000, {"type": "subscribed"})) + "\n"
    digest = hashlib.sha256(body.encode()).hexdigest()
    for release in mod.RELEASE_IDS:
        base = tmp_path / "releases" / release
        path = base / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        (base / ".VERIFIED.json").write_text(json.dumps({
            "release_id": release,
            "version_binding_mode": "VERSION_BOUND",
            "evidence_tier": mod.EVIDENCE,
        }), encoding="utf-8")
        (base / "MANIFEST.json").write_text(json.dumps({
            "objects": [{"key": key, "sha256": digest, "size": len(body.encode())}]
        }), encoding="utf-8")
        # Glob-visible but not manifest-bound: discovery must ignore it.
        extra = base / "raw_rfq/date=2026-07-13/rfq_01.ndjson"
        extra.write_text(body, encoding="utf-8")
    found = mod.discover_inputs(tmp_path)
    assert found["logical_manifest_bindings"] == 2
    assert found["objects"] == 1
    assert found["deduplicated_overlapping_objects"] == 1
    assert found["overlap_keys"] == [key]

    second = tmp_path / "releases" / mod.RELEASE_IDS[1] / "MANIFEST.json"
    second.write_text(json.dumps({
        "objects": [{"key": key, "sha256": "f" * 64, "size": len(body.encode())}]
    }), encoding="utf-8")
    try:
        mod.discover_inputs(tmp_path)
    except mod.RFQStageError as exc:
        assert "conflicting bytes" in str(exc)
    else:
        raise AssertionError("same key with a different manifest SHA must fail")


@pytest.mark.parametrize(
    "mutation",
    ["forged_sha", "unbound_version", "unbound_execution", "receipt_key", "zero_invalid"],
)
def test_quarantine_mutations_and_unbound_evidence_fail_closed(tmp_path, mutation):
    fixture = quarantine_fixture(tmp_path)
    declaration = json.loads(json.dumps(fixture["declaration"]))
    receipt = json.loads(json.dumps(fixture["receipt"]))
    if mutation == "forged_sha":
        declaration["quarantined_objects"][0]["sha256"] = "f" * 64
    elif mutation == "unbound_version":
        declaration["quarantined_objects"][0]["version_id"] = "forged-version"
    elif mutation == "unbound_execution":
        declaration["source_execution_commit"] = "9" * 40
    elif mutation == "receipt_key":
        receipt["key"] += ".forged"
    elif mutation == "zero_invalid":
        receipt["invalid_line_count"] = 0
        receipt["invalid_lines"] = []
    write_json(fixture["run_dir"] / mod.QUARANTINE_DECLARATION, declaration)
    write_json(fixture["run_dir"] / mod.MALFORMED_OBJECT_RECEIPT, receipt)
    discovered = mod.discover_inputs(fixture["cache"])
    with pytest.raises(mod.RFQStageError):
        mod.apply_object_quarantine(
            fixture["run_dir"], fixture["manifest"], discovered
        )


def test_receipt_without_preregistered_declaration_is_rejected(tmp_path):
    fixture = quarantine_fixture(tmp_path)
    (fixture["run_dir"] / mod.QUARANTINE_DECLARATION).unlink()
    with pytest.raises(mod.RFQStageError, match="requires both"):
        mod.apply_object_quarantine(
            fixture["run_dir"], fixture["manifest"],
            mod.discover_inputs(fixture["cache"]),
        )


def test_registered_code_repair_binds_archived_failure_and_scratch_receipts(tmp_path):
    fixture = quarantine_fixture(tmp_path)
    run_dir = fixture["run_dir"]
    discovered_full_set = mod.discover_inputs(fixture["cache"])
    declaration_source = run_dir / mod.QUARANTINE_DECLARATION
    receipt_source = run_dir / mod.MALFORMED_OBJECT_RECEIPT
    archived_declaration = run_dir / mod.REPAIR_DECLARATION_ARCHIVE
    archived_receipt = run_dir / mod.REPAIR_RECEIPT_ARCHIVE
    archived_declaration.parent.mkdir(parents=True, exist_ok=True)
    archived_declaration.write_bytes(declaration_source.read_bytes())
    archived_receipt.write_bytes(receipt_source.read_bytes())
    failed_state = run_dir / mod.FAILED_STATE_ARCHIVE
    original_scratch = "/srv/w09/runs/test-run/cache/rfq_full_scratch.duckdb"
    failed_input_fingerprint = discovered_full_set["path_size_fingerprint_sha256"]
    write_json(failed_state, {
        "schema": "rfq-full-stage-state-v1",
        "status": "FAILED_RESUMABLE",
        "resume": False,
        "scratch": original_scratch,
        "input_fingerprint": failed_input_fingerprint,
        "error": f"Malformed JSON in {fixture['known_key']}",
    })
    failed_resource = run_dir / mod.FAILED_RESOURCE_ARCHIVE
    write_json(failed_resource, {
        "schema_version": "w09-stage-resource-v1",
        "label": "rfq_full_stage",
        "return_code": 1,
        "command": ["python", "rfq_full_stage.py"],
    })
    failed_scratch = run_dir / mod.FAILED_SCRATCH_RECEIPT_ARCHIVE
    write_json(failed_scratch, {
        "schema_version": "rfq-failed-scratch-receipt-v1",
        "run_id": run_dir.name,
        "original_scratch_path": original_scratch,
        "preserved_scratch_path": "/srv/w09/runs/test-run/cache/rfq_full_scratch.failed.duckdb",
        "sha256": "4" * 64,
        "bytes": 4096,
        "mtime_utc": "2026-07-15T13:20:32Z",
        "input_fingerprint": failed_input_fingerprint,
        "disposition": "PRESERVED_RENAMED_NO_RESUME",
        "resume_allowed": False,
    })
    failed_input_identity = run_dir / mod.FAILED_INPUT_IDENTITY_ARCHIVE
    write_json(
        failed_input_identity,
        mod._legacy_full_input_identity(discovered_full_set),
    )
    old_commit = fixture["declaration"]["source_execution_commit"]
    new_commit = "6" * 40
    repository = fixture["manifest"]["repository"]
    repository.update({
        "initial_execution_commit": old_commit,
        "previous_execution_commit": old_commit,
        "execution_commit": new_commit,
        "source_manifest_sha256": "7" * 64,
        "query_set_sha256": "8" * 64,
    })
    repair = {
        "repair_id": "repair-01",
        "finding": "MALFORMED_NDJSON_OBJECT",
        "pre_repair_status": "CYCLE1_CORE_COMPLETE_RFQ_FULL_SCAN_PENDING",
        "rfq_result_state": "NO_RFQ_RESULT_OPENED",
        "quarantine_policy": "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE",
        "previous_execution_commit": old_commit,
        "current_execution_commit": new_commit,
        "previous_source_manifest_sha256": "2" * 64,
        "current_source_manifest_sha256": "7" * 64,
        "previous_query_set_sha256": "3" * 64,
        "current_query_set_sha256": "8" * 64,
        "core_result_disposition": "CORE_RESULTS_PRESERVED_NOT_RECOMPUTED",
        "core_results_recomputed": False,
        "registration_change_class": (
            "DATA_INTEGRITY_HANDLING_ONLY_NO_HYPOTHESIS_DESIGN_CHANGE"
        ),
        "hypothesis_design_change": "NONE",
        "data_integrity_handling_change": (
            "ONE_EXACT_WHOLE_OBJECT_QUARANTINE_AND_CONSERVATIVE_GAP_CENSORING"
        ),
        "frozen_design_change": (
            "NO_HYPOTHESIS_DESIGN_CHANGE; DATA_INTEGRITY_HANDLING_CHANGED"
        ),
        "threshold_feature_test_or_hypothesis_status_changed": False,
        "declaration_input_path": mod.QUARANTINE_DECLARATION,
        "declaration_path": mod.REPAIR_DECLARATION_ARCHIVE,
        "declaration_sha256": mod.sha256(archived_declaration),
        "receipt_input_path": mod.MALFORMED_OBJECT_RECEIPT,
        "receipt_path": mod.REPAIR_RECEIPT_ARCHIVE,
        "receipt_sha256": mod.sha256(archived_receipt),
        "failed_state_path": mod.FAILED_STATE_ARCHIVE,
        "failed_state_sha256": mod.sha256(failed_state),
        "failed_resource_receipt_path": mod.FAILED_RESOURCE_ARCHIVE,
        "failed_resource_receipt_sha256": mod.sha256(failed_resource),
        "failed_scratch_receipt_path": mod.FAILED_SCRATCH_RECEIPT_ARCHIVE,
        "failed_scratch_receipt_sha256": mod.sha256(failed_scratch),
        "failed_input_identity_path": mod.FAILED_INPUT_IDENTITY_ARCHIVE,
        "failed_input_identity_sha256": mod.sha256(failed_input_identity),
        "quarantined_objects": fixture["declaration"]["quarantined_objects"],
    }
    fixture["manifest"]["data_integrity_repairs"] = [repair]
    inputs = mod.apply_object_quarantine(
        run_dir, fixture["manifest"], mod.discover_inputs(fixture["cache"])
    )
    assert inputs["failed_attempt_binding"] == {
        "repair_id": "repair-01",
        "failed_state_path": mod.FAILED_STATE_ARCHIVE,
        "failed_state_sha256": repair["failed_state_sha256"],
        "failed_resource_receipt_path": mod.FAILED_RESOURCE_ARCHIVE,
        "failed_resource_receipt_sha256": repair["failed_resource_receipt_sha256"],
        "failed_scratch_receipt_path": mod.FAILED_SCRATCH_RECEIPT_ARCHIVE,
        "failed_scratch_receipt_sha256": repair["failed_scratch_receipt_sha256"],
        "failed_input_identity_path": mod.FAILED_INPUT_IDENTITY_ARCHIVE,
        "failed_input_identity_sha256": repair["failed_input_identity_sha256"],
    }
    forged_failed_identity = mod._legacy_full_input_identity(discovered_full_set)
    forged_failed_identity["unique_objects"] += 1
    write_json(failed_input_identity, forged_failed_identity)
    repair["failed_input_identity_sha256"] = mod.sha256(failed_input_identity)
    with pytest.raises(mod.RFQStageError, match="manifest-derived full set"):
        mod.apply_object_quarantine(
            run_dir, fixture["manifest"], mod.discover_inputs(fixture["cache"])
        )
    write_json(
        failed_input_identity,
        mod._legacy_full_input_identity(discovered_full_set),
    )
    repair["failed_input_identity_sha256"] = mod.sha256(failed_input_identity)
    repair["hypothesis_design_change"] = "FORGED_DESIGN_CHANGE"
    with pytest.raises(
        mod.RFQStageError,
        match="RFQ structural repair binding mismatch: hypothesis_design_change",
    ):
        mod.apply_object_quarantine(
            run_dir, fixture["manifest"], mod.discover_inputs(fixture["cache"])
        )
    repair["hypothesis_design_change"] = "NONE"
    fixture["manifest"]["data_integrity_repairs"][0]["failed_state_sha256"] = "0" * 64
    with pytest.raises(mod.RFQStageError, match="failed state SHA"):
        mod.apply_object_quarantine(
            run_dir, fixture["manifest"], mod.discover_inputs(fixture["cache"])
        )


def test_quarantine_without_two_valid_gap_neighbors_is_rejected(tmp_path):
    fixture = quarantine_fixture(tmp_path)
    discovered = mod.discover_inputs(fixture["cache"])
    pre_key = "raw_rfq/date=2026-07-13/rfq_23.ndjson"
    discovered["objects_detail"] = [
        row for row in discovered["objects_detail"] if row["key"] != pre_key
    ]
    with pytest.raises(mod.RFQStageError, match="preceding and following"):
        mod.apply_object_quarantine(
            fixture["run_dir"], fixture["manifest"], discovered
        )


def test_manifest_bound_whole_object_quarantine_is_gap_safe(tmp_path):
    fixture = quarantine_fixture(tmp_path)
    discovered = mod.discover_inputs(fixture["cache"])
    inputs = mod.apply_object_quarantine(
        fixture["run_dir"], fixture["manifest"], discovered
    )
    assert inputs["coverage_status"] == "PARTIAL_OBJECT_COVERAGE_QUARANTINED"
    assert inputs["full_object_coverage"] is False
    assert inputs["unique_objects_total"] == 5
    assert inputs["consumed_unique_objects"] == 4
    assert inputs["quarantined_unique_objects"] == 1
    assert inputs["quarantined_bytes"] == fixture["known_path"].stat().st_size
    assert mod.stage_completion_status(inputs) == (
        "COMPLETE_PARTIAL_OBJECT_COVERAGE_QUARANTINED"
    )
    assert fixture["known_path"] not in inputs["paths"]
    assert all(row["key"] != fixture["known_key"] for row in inputs["objects_detail"])
    assert fixture["receipt_path"] in inputs["paths"]
    assert any(row["key"] == fixture["receipt_key"] for row in inputs["objects_detail"])

    connection = duckdb.connect()
    connection.execute("SET TimeZone='UTC'")
    # The excluded file is malformed outer NDJSON; success proves it was never opened.
    mod.build_scan_tables(
        connection, inputs["paths"], fixture["run_dir"].name,
        inputs["quarantine_boundaries"],
    )
    gap = connection.execute("""
      SELECT gap_start_ns,gap_end_ns FROM rfq_quarantine_gaps
    """).fetchone()
    assert gap == (
        fixture["base_ns"] + 2_000_000_001,
        fixture["base_ns"] + 7_204_000_000_000,
    )
    assert connection.execute("""
      SELECT count(*) FROM rfq_observation_boundaries
      WHERE contains(reason,'WHOLE_OBJECT_QUARANTINE_MALFORMED_NDJSON')
    """).fetchone()[0] == 2

    mod.build_request_tables(connection, fixture["run_dir"].name)
    lifecycle = connection.execute("""
      SELECT endpoint_type,delete_observed,observation_boundary_reason
      FROM rfq_lifecycle_base WHERE rfq_id_hash=sha256('CROSS-GAP')
    """).fetchone()
    assert lifecycle[0] == "RIGHT_CENSORED_AT_OBSERVATION_BOUNDARY"
    assert lifecycle[1] is False
    assert "WHOLE_OBJECT_QUARANTINE_MALFORMED_NDJSON_GAP_START" in lifecycle[2]
    assert connection.execute("""
      SELECT count(*) FROM rfq_unmatched_deletes
      WHERE disposition='CROSS_OBSERVATION_BOUNDARY_DELETE'
    """).fetchone()[0] == 1

    core_path = tmp_path / "quarantine-core.duckdb"
    core = duckdb.connect(str(core_path))
    core.execute("""
      CREATE TABLE universe(date DATE,market_ticker VARCHAR,sport VARCHAR,league VARCHAR,
        root_event_id VARCHAR,root_map_status VARCHAR,occurrence_datetime TIMESTAMPTZ,
        dim_effective_us BIGINT)
    """)
    base_us = fixture["base_ns"] // 1000
    core.executemany("""
      INSERT INTO universe VALUES (DATE '2026-07-14',?,'Soccer','L',?,
        'PROVISIONAL_HEURISTIC_MATCHUP_TIME',
        TIMESTAMPTZ '2026-07-14 03:00:00+00',?)
    """, [("M-A", "ROOT-A", base_us - 3_600_000_000),
           ("M-B", "ROOT-B", base_us - 3_600_000_000)])
    core.execute("""
      CREATE TABLE l1_real(market_ticker VARCHAR,t_us BIGINT,recv_mono_ns BIGINT,
        yes_bid_e4 INTEGER,yes_ask_e4 INTEGER,yes_bid_qty_e4 BIGINT,yes_ask_qty_e4 BIGINT)
    """)
    l1 = []
    for market, anchor in (("M-A", base_us + 1_000_000),
                           ("M-B", base_us + 7_204_500_000)):
        for offset_s in range(-700, 132):
            t_us = anchor + offset_s * 1_000_000
            l1.append((market, t_us, t_us * 1000, 3900, 4100, 100_000, 100_000))
    core.executemany("INSERT INTO l1_real VALUES (?,?,?,?,?,?,?)", l1)
    core.execute(
        "CREATE TABLE trades_safe(market_ticker VARCHAR,t_us BIGINT,"
        "taker_sign INTEGER,count_e4 BIGINT)"
    )
    core.execute("CREATE TABLE l2_all(market_ticker VARCHAR,t_us BIGINT)")
    core.execute("CREATE TABLE capture_gaps(start_us BIGINT,end_us BIGINT)")
    core.close()

    mod.attach_core_and_enrich(connection, core_path)
    mod.build_descriptive_tables(connection)
    # Both creates are in different observation segments: no interarrival crosses the gap.
    assert connection.execute("SELECT coalesce(sum(n),0) FROM rfq_interarrival").fetchone()[0] == 0
    missing_hour = connection.execute("""
      SELECT requests_in_consumed_objects,object_coverage_status,zero_interpretation
      FROM rfq_hour_coverage WHERE hour_start_us=?
    """, [base_us + 3_600_000_000]).fetchone()
    assert missing_hour == (
        0, "PARTIAL_OBJECT_COVERAGE_QUARANTINED",
        "NOT_AN_OBSERVED_ZERO_QUARANTINE_OVERLAP",
    )
    for table in (
        "rfq_flow_daily", "rfq_event_flow_hourly", "rfq_flow_hourly",
        "rfq_flow_minute", "rfq_burst_summary",
    ):
        assert "object_coverage_status" in {
            row[1] for row in connection.execute(f"PRAGMA table_info('{table}')").fetchall()
        }

    mod.build_clob_context(connection, max_per_root=50)
    invalid_context = connection.execute("""
      SELECT count(*) FROM rfq_clob_context
      WHERE market_ticker='M-B' AND endpoint_type='CREATE' AND cohort='RFQ'
        AND window_label='m1_0' AND NOT valid_receive_window
    """).fetchone()[0]
    assert invalid_context == 1, {
        "requests": connection.execute(
            "SELECT market_ticker,create_date,dimension_causality FROM rfq_requests"
        ).fetchall(),
        "anchors": connection.execute(
            "SELECT market_ticker,endpoint_type,anchor_role FROM rfq_clob_anchors"
        ).fetchall(),
        "context": connection.execute(
            "SELECT market_ticker,endpoint_type,cohort,window_label,valid_receive_window "
            "FROM rfq_clob_context_unmatched WHERE market_ticker='M-B'"
        ).fetchall(),
    }
    control = connection.execute("""
      SELECT prior_control_lookback_observed,future_control_outcome_observed,matched
      FROM rfq_control_balance
      WHERE market_ticker='M-B' AND endpoint_type='CREATE' AND anchor_role='REQUEST_MARKET'
    """).fetchone()
    assert control == (False, False, False)
    inputs["run_id"] = fixture["run_dir"].name
    inputs["cycle1_duckdb_binding"] = synthetic_cycle1_binding(
        fixture["run_dir"].name
    )
    summary = mod.build_summary(connection, inputs, {
        "candidate_anchor_markets": 0,
    }, 1.0)
    assert summary["run_id"] == fixture["run_dir"].name
    assert summary["input"]["cycle1_duckdb_binding"] == inputs[
        "cycle1_duckdb_binding"
    ]
    assert summary["status"] == "PARTIAL_OBJECT_COVERAGE_QUARANTINED"
    assert summary["analysis_scope"] == "DESCRIPTIVE_DISCOVERY_ONLY"
    assert summary["input"]["whole_object_quarantine"] is True
    assert summary["input"]["line_salvage"] is False
    assert summary["coverage"]["quarantine_gap_count"] == 1
    source_text = path.read_text(encoding="utf-8")
    assert "Full-scan RFQ" not in source_text
    assert "# Full RFQ exploratory stage" not in source_text
    connection.close()


def test_adjacent_double_quarantine_is_one_contiguous_gap_with_two_boundaries(
    tmp_path,
):
    base_ns = 10_000_000_000
    before = tmp_path / "raw_rfq/date=2026-07-13/rfq_23.ndjson.1"
    after = tmp_path / "raw_rfq/date=2026-07-14/rfq_00.ndjson.1"
    write_rows(before, [
        recorder(base_ns, frame(
            "rfq_created", "CROSS-DOUBLE-GAP", "M-A", "2026-07-13T23:59:59Z"
        )),
        recorder(base_ns + 1_000_000_000, {"type": "subscribed", "sid": 7}),
    ])
    write_rows(after, [
        recorder(base_ns + 8_000_000_000, frame(
            "rfq_deleted", "CROSS-DOUBLE-GAP", "M-A", "2026-07-14T00:00:07Z"
        )),
        recorder(base_ns + 9_000_000_000, frame(
            "rfq_created", "AFTER", "M-B", "2026-07-14T00:00:08Z"
        )),
    ])
    bad_keys = [
        "raw_rfq/date=2026-07-13/rfq_23.ndjson.2",
        "raw_rfq/date=2026-07-14/rfq_00.ndjson",
    ]
    boundary = {
        "release_id": mod.RELEASE_IDS[1],
        "key": bad_keys[0],
        "sha256": "1" * 64,
        "quarantined_keys": bad_keys,
        "quarantined_object_set_sha256": "2" * 64,
        "previous_path": before,
        "next_path": after,
    }
    connection = duckdb.connect()
    mod.build_scan_tables(connection, [before, after], "double-gap", [boundary])
    gap = connection.execute("""
      SELECT key,quarantined_keys_json,quarantined_object_count,
        quarantined_object_set_sha256,gap_start_ns,gap_end_ns
      FROM rfq_quarantine_gaps
    """).fetchone()
    assert gap == (
        bad_keys[0],
        json.dumps(bad_keys, separators=(",", ":")),
        2,
        "2" * 64,
        base_ns + 1_000_000_001,
        base_ns + 8_000_000_000,
    )
    assert connection.execute("""
      SELECT count(*) FROM rfq_observation_boundaries
      WHERE contains(reason,'WHOLE_OBJECT_QUARANTINE_MALFORMED_NDJSON')
    """).fetchone()[0] == 2
    mod.build_request_tables(connection, "double-gap")
    lifecycle = connection.execute("""
      SELECT endpoint_type,delete_observed,observation_boundary_reason
      FROM rfq_lifecycle_base
      WHERE rfq_id_hash=sha256('CROSS-DOUBLE-GAP')
    """).fetchone()
    assert lifecycle[0] == "RIGHT_CENSORED_AT_OBSERVATION_BOUNDARY"
    assert lifecycle[1] is False
    assert "_GAP_START" in lifecycle[2]
    assert connection.execute("""
      SELECT count(*) FROM rfq_unmatched_deletes
      WHERE disposition='CROSS_OBSERVATION_BOUNDARY_DELETE'
    """).fetchone()[0] == 1
    connection.close()


def test_repair02_selection_fingerprint_is_deterministic_and_binds_evidence():
    rows = [
        {"key": "b", "size": 2, "sha256": "2" * 64},
        {"key": "a", "size": 1, "sha256": "1" * 64},
        {"key": "c", "size": 3, "sha256": "3" * 64},
    ]
    first = mod._repair02_selection_fingerprint(
        "f" * 64, rows[1:], rows[:1], "d" * 64, ["a" * 64, "b" * 64]
    )
    assert first == mod._repair02_selection_fingerprint(
        "f" * 64, list(reversed(rows[1:])), rows[:1], "d" * 64,
        ["a" * 64, "b" * 64],
    )
    assert first != mod._repair02_selection_fingerprint(
        "f" * 64, rows[1:], rows[:1], "e" * 64, ["a" * 64, "b" * 64]
    )
    assert first != mod._repair02_selection_fingerprint(
        "f" * 64, rows[1:], rows[:1], "d" * 64, ["b" * 64, "a" * 64]
    )


def test_repair02_contract_excludes_exactly_two_adjacent_whole_objects(
    tmp_path, monkeypatch
):
    fixture = double_quarantine_contract_fixture(tmp_path)
    monkeypatch.setattr(
        mod, "apply_object_quarantine",
        lambda run_dir, manifest, inputs, _ignore_repair02=False: fixture["first"],
    )
    monkeypatch.setattr(
        mod, "_validate_repair02_archives",
        lambda *args: ([{"repair_id": "repair-01"}, {"repair_id": "repair-02"}],
                       [{"repair_id": "repair-01"}, {"repair_id": "repair-02"}]),
    )
    result = mod._apply_double_object_quarantine(
        fixture["run_dir"], fixture["manifest"], fixture["inputs"]
    )
    assert result["consumed_unique_objects"] == 2
    assert result["consumed_logical_bindings"] == 2
    assert result["quarantined_unique_objects"] == 2
    assert result["quarantined_logical_bindings"] == 2
    assert result["selection_fingerprint_sha256"] == fixture["selection"]
    assert [row["key"] for row in result["quarantine_details"]] == [
        "raw_rfq/date=2026-07-13/rfq_23.ndjson.2",
        "raw_rfq/date=2026-07-14/rfq_00.ndjson",
    ]
    assert len(result["quarantine_boundaries"]) == 1
    assert result["quarantine_boundaries"][0]["quarantined_keys"] == [
        "raw_rfq/date=2026-07-13/rfq_23.ndjson.2",
        "raw_rfq/date=2026-07-14/rfq_00.ndjson",
    ]
    assert result["expected_success_resource"] == {
        "label": "rfq_full_stage_repair02",
        "path": "logs/resources/rfq_full_stage_repair02.json",
    }


def test_repair03_consumer_replays_same_selection_with_parser_binding(
    tmp_path, monkeypatch
):
    fixture = repair03_consumer_fixture(tmp_path, monkeypatch)
    observed = {}

    def replay(run_dir, manifest, inputs, registry_boundary, enforce_pairs):
        observed.update({
            "run_dir": run_dir,
            "manifest": manifest,
            "inputs": inputs,
            "registry_boundary": registry_boundary,
            "enforce_pairs": enforce_pairs,
        })
        return copy.deepcopy(fixture["base_result"])

    monkeypatch.setattr(mod, "_apply_double_object_quarantine", replay)
    result = mod.apply_object_quarantine(
        fixture["run_dir"], fixture["manifest"], fixture["inputs"]
    )
    assert observed["manifest"]["data_integrity_repairs"] == fixture[
        "manifest"
    ]["data_integrity_repairs"][:2]
    assert observed["registry_boundary"] == (
        fixture["run_dir"] / mod.REPAIR03_PRE_ROOT / "TRIAL_REGISTRY.jsonl"
    ).read_bytes()
    assert observed["enforce_pairs"] is False
    assert result["selection_fingerprint_sha256"] == fixture[
        "base_result"
    ]["selection_fingerprint_sha256"]
    assert [row["repair_id"] for row in result["repair_chain"]] == [
        "repair-01", "repair-02", "repair-03",
    ]
    assert set(result["repair_chain"][2]) == {
        "repair_id", "registration_path", "registration_sha256",
        "parser_contract_path", "parser_contract_sha256",
        "authority_basis_path", "authority_basis_sha256",
        "inner_payload_audit_path", "inner_payload_audit_sha256",
    }
    assert [row["repair_id"] for row in result["failed_attempt_bindings"]] == [
        "repair-01", "repair-02", "repair-03",
    ]
    assert set(result["failed_attempt_bindings"][2]) == {
        "repair_id", "attempt_id", "failed_state_path", "failed_state_sha256",
        "failed_resource_receipt_path", "failed_resource_receipt_sha256",
        "failed_scratch_receipt_path", "failed_scratch_receipt_sha256",
        "failed_input_identity_path", "failed_input_identity_sha256",
        "input_fingerprint", "resource_label", "retry_requirement",
    }
    assert result["inner_payload_parser_contract"] == {
        "path": mod.REPAIR03_PARSER_CONTRACT,
        "sha256": fixture["repair03"]["parser_contract_sha256"],
        "schema_version": mod.REPAIR03_PARSER_SCHEMA,
    }
    assert result["registered_rfq_query_sha256"] == fixture["repair03"][
        "registered_rfq_query_sha256"
    ]
    assert result["expected_success_resource"] == {
        "label": mod.REPAIR03_SUCCESS_RESOURCE_LABEL,
        "path": mod.REPAIR03_SUCCESS_RESOURCE_PATH,
    }


def test_repair03_strict_parent_replay_uses_repair04_boundary_snapshot(
    tmp_path, monkeypatch
):
    fixture = repair03_consumer_fixture(tmp_path, monkeypatch)
    run_dir = fixture["run_dir"]
    boundary = run_dir / "synthetic-repair04-parent-boundary"
    repository = fixture["manifest"]["repository"]
    for relative in (
        "SOURCE_MANIFEST.json", "SOURCE_SHA256SUMS.txt", "QUERY_SHA256SUMS.txt",
        *repository["query_files"], "TRIAL_REGISTRY.jsonl",
    ):
        source = run_dir / relative
        target = boundary / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    # A legitimate attempt after repair-03 replaces these active evidence
    # documents.  The descendant replay must use repair-03's fixed archives,
    # while source/query/registry bytes come from repair-04's pre-repair snapshot.
    write_json(
        run_dir / "REPORT/tables/RFQ_FULL_STAGE_STATE.json",
        {"status": "ATTEMPT_04_REPLACED_ACTIVE_PARENT_EVIDENCE"},
    )
    write_json(
        run_dir / "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json",
        {"schema": "rfq-full-input-identity-v3", "attempt": 4},
    )
    monkeypatch.setattr(
        mod,
        "_apply_double_object_quarantine",
        lambda *_args, **_kwargs: copy.deepcopy(fixture["base_result"]),
    )
    result = mod._apply_repair03_parser_contract(
        run_dir,
        fixture["manifest"],
        fixture["inputs"],
        descendant_boundary_root=boundary,
    )
    assert [row["repair_id"] for row in result["repair_chain"]] == [
        "repair-01", "repair-02", "repair-03",
    ]
    assert result["registered_rfq_query_sha256"] == fixture["repair03"][
        "registered_rfq_query_sha256"
    ]


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("record", "field set"),
        ("audit", "archive inventory"),
        ("registry", "transaction mutation"),
        ("attestation", "attestation binding"),
        ("parser", "SHA-256 binding"),
    ),
)
def test_repair03_consumer_mutations_fail_closed(
    tmp_path, monkeypatch, mutation, message
):
    fixture = repair03_consumer_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mod,
        "_apply_double_object_quarantine",
        lambda *_args: copy.deepcopy(fixture["base_result"]),
    )
    if mutation == "record":
        fixture["manifest"]["data_integrity_repairs"][2]["unregistered"] = True
    elif mutation == "audit":
        audit = json.loads(
            (fixture["run_dir"] / mod.REPAIR03_AUDIT_ARCHIVE).read_text()
        )
        audit["unexpected_inner_payload_row_count"] = 1
        write_json(fixture["run_dir"] / mod.REPAIR03_AUDIT_ARCHIVE, audit)
    elif mutation == "registry":
        registry = fixture["run_dir"] / "TRIAL_REGISTRY.jsonl"
        registry.write_bytes(registry.read_bytes() + b'{"unregistered":true}\n')
    elif mutation == "attestation":
        attestation = json.loads(
            (fixture["run_dir"] / mod.REPAIR03_SOURCE_ATTESTATION).read_text()
        )
        attestation["git_tree"] = "f" * 40
        write_json(fixture["run_dir"] / mod.REPAIR03_SOURCE_ATTESTATION, attestation)
    elif mutation == "parser":
        contract = json.loads(
            (fixture["run_dir"] / mod.REPAIR03_PARSER_CONTRACT).read_text()
        )
        contract["line_salvage"] = True
        write_json(fixture["run_dir"] / mod.REPAIR03_PARSER_CONTRACT, contract)
    with pytest.raises(mod.RFQStageError, match=message):
        mod.apply_object_quarantine(
            fixture["run_dir"], fixture["manifest"], fixture["inputs"]
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("top_level", "exact approved retained set"),
        ("summary_missing", "object summaries are incomplete"),
        ("object_identity", "object identity/contract mismatch"),
        ("object_totals", "object totals do not reconcile"),
    ),
)
def test_repair03_audit_schema_and_object_mutations_fail_closed(
    tmp_path, monkeypatch, mutation, message
):
    fixture = repair03_consumer_fixture(tmp_path, monkeypatch)
    audit = json.loads(
        (fixture["run_dir"] / mod.REPAIR03_AUDIT_ARCHIVE).read_text()
    )
    active_input_path = (
        fixture["run_dir"] / "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json"
    )
    active_input = json.loads(active_input_path.read_text())
    if mutation == "top_level":
        audit["unregistered"] = True
    elif mutation == "summary_missing":
        audit["object_summaries"].pop()
    elif mutation == "object_identity":
        audit["object_summaries"][0]["expected_sha256"] = "9" * 64
        audit["object_summaries"][0]["observed_sha256"] = "9" * 64
    elif mutation == "object_totals":
        audit["object_summaries"][0]["total_lines"] += 1
        audit["object_summaries"][0]["data_frame_rows"] += 1
    with pytest.raises(mod.RFQStageError, match=message):
        mod._validate_repair03_inner_payload_audit(
            audit,
            run_id=fixture["run_dir"].name,
            active_input=active_input,
            active_input_sha256=mod.sha256(active_input_path),
            audit_source_sha256=mod.REPAIR03_APPROVED_AUDIT_SOURCE_SHA256,
            base_result=fixture["base_result"],
        )


def test_explicit_null_cannot_be_blessed_by_coordinated_repair03_rewrite(
    tmp_path, monkeypatch
):
    fixture = repair03_consumer_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mod,
        "_apply_double_object_quarantine",
        lambda *_args, **_kwargs: copy.deepcopy(fixture["base_result"]),
    )
    run_dir = fixture["run_dir"]
    # Typed DuckDB JSON maps this explicit null marker to the same SQL NULL as
    # a missing field.  The preregistration evidence, not scan_sql in isolation,
    # must therefore reject any attempt to bless these bytes.
    compact = b'{"marker":null,"raw":"{}"}'
    explicit_null_payload = compact + b" " * (39 - len(compact)) + b"\n"
    assert len(explicit_null_payload) == 40
    explicit_null_path = run_dir / "explicit-null-marker.ndjson"
    explicit_null_path.write_bytes(explicit_null_payload)
    rewritten_sha = hashlib.sha256(explicit_null_payload).hexdigest()

    for relative in (
        "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json",
        mod.REPAIR03_FAILED_INPUT_ARCHIVE,
    ):
        identity_path = run_dir / relative
        identity = json.loads(identity_path.read_text())
        identity["consumed_objects"][1]["sha256"] = rewritten_sha
        write_json(identity_path, identity)
    rewritten_input_sha = mod.sha256(
        run_dir / "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json"
    )
    for relative in (
        "DATA_INTEGRITY/RFQ_INNER_PAYLOAD_CONTRACT_AUDIT_03.json",
        mod.REPAIR03_AUDIT_ARCHIVE,
    ):
        audit_path = run_dir / relative
        audit = json.loads(audit_path.read_text())
        audit["input_identity_sha256"] = rewritten_input_sha
        audit["object_summaries"][1]["expected_sha256"] = rewritten_sha
        audit["object_summaries"][1]["observed_sha256"] = rewritten_sha
        write_json(audit_path, audit)
    rewritten_audit_sha = mod.sha256(run_dir / mod.REPAIR03_AUDIT_ARCHIVE)

    repair03 = fixture["manifest"]["data_integrity_repairs"][2]
    repair03["failed_input_identity_sha256"] = rewritten_input_sha
    repair03["inner_payload_audit_sha256"] = rewritten_audit_sha
    repair03["failed_attempt"]["input_identity_sha256"] = rewritten_input_sha
    repair03["inner_payload_audit"]["sha256"] = rewritten_audit_sha
    repair03["archive_inventory"] = mod._archive_inventory(
        run_dir / mod.REPAIR03_PRE_ROOT
    )
    receipt = {
        key: value for key, value in repair03.items()
        if key not in {"repair_receipt_path", "repair_receipt_sha256"}
    }
    write_json(run_dir / mod.REPAIR03_REGISTRATION, receipt)
    repair03["repair_receipt_sha256"] = mod.sha256(
        run_dir / mod.REPAIR03_REGISTRATION
    )

    with pytest.raises(mod.RFQStageError, match="approved evidence identity changed"):
        mod.apply_object_quarantine(
            run_dir, fixture["manifest"], fixture["inputs"]
        )


def test_repair04_resource_contract_and_authority_are_exact(tmp_path):
    run_dir = tmp_path / "repair04-contract"
    run_dir.mkdir()
    applied_at = "2026-07-15T18:00:00Z"
    repair04 = {
        "failed_state_sha256": "1" * 64,
        "failed_resource_receipt_sha256": "2" * 64,
        "failed_input_identity_sha256": "3" * 64,
        "failed_scratch_receipt_sha256": "4" * 64,
        "resource_contract_path": mod.REPAIR04_RESOURCE_CONTRACT_PATH,
        "authority_basis_path": mod.REPAIR04_AUTHORITY_BASIS,
    }
    contract = mod._expected_repair04_resource_contract(
        run_dir.name, applied_at, repair04
    )
    write_json(run_dir / mod.REPAIR04_RESOURCE_CONTRACT_PATH, contract)
    contract_sha = mod.sha256(run_dir / mod.REPAIR04_RESOURCE_CONTRACT_PATH)
    repair04["resource_contract_sha256"] = contract_sha
    authority = {
        "schema_version": mod.REPAIR04_AUTHORITY_SCHEMA,
        "run_id": run_dir.name,
        "recorded_at_utc": applied_at,
        "mission_sha256": mod.EXPECTED_MISSION_SHA256,
        "authority_class": mod.REPAIR04_AUTHORITY_CLASS,
        "permitted_change": "EXISTING_W09_EXECUTION_RESOURCE_RETUNE_ONLY",
        "resource_contract_path": mod.REPAIR04_RESOURCE_CONTRACT_PATH,
        "resource_contract_sha256": contract_sha,
        "existing_w09_instance_id": mod.EXPECTED_INSTANCE,
        "new_instance_spend_authorized": False,
        "instance_resize_authorized": False,
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "hypothesis_design_change": "NONE",
        "dependent_rfq_result_opened": False,
    }
    write_json(run_dir / mod.REPAIR04_AUTHORITY_BASIS, authority)
    repair04["authority_basis_sha256"] = mod.sha256(
        run_dir / mod.REPAIR04_AUTHORITY_BASIS
    )

    observed_contract, observed_authority = (
        mod._validate_repair04_contract_artifacts(run_dir, repair04, applied_at)
    )
    assert observed_contract == contract
    assert observed_authority == authority
    assert contract["current_runtime"] == mod.REPAIR04_RUNTIME_CONTRACT
    assert contract["previous_runtime"] == mod.REPAIR04_PREVIOUS_RESOURCE_CONTRACT
    assert contract["expected_command"] == mod._repair04_retry_command(run_dir.name)
    assert contract["expected_command"][1] == (
        f"/srv/w09-research/runs/{run_dir.name}/queries/rfq_full_stage.py"
    )
    assert mod._repair04_failed_command(run_dir.name)[1] == (
        f"/srv/w09-research/runs/{run_dir.name}/source/rfq_full_stage.py"
    )
    assert contract["disk_safety"]["failed_scratch_deletion_allowed"] is False
    assert contract["w09"]["resize_allowed"] is False


def repair04_runtime_enforcement_fixture(tmp_path, monkeypatch):
    run_dir = tmp_path / "repair04-runtime-enforcement"
    execution_entry = run_dir / mod.REPAIR04_EXECUTION_QUERY
    execution_entry.parent.mkdir(parents=True)
    execution_entry.write_bytes(path.read_bytes())
    monkeypatch.setattr(mod, "__file__", str(execution_entry))
    binding = {
        "path": mod.REPAIR04_RESOURCE_CONTRACT_PATH,
        "sha256": "a" * 64,
        "schema_version": mod.REPAIR04_RESOURCE_SCHEMA,
        "current_runtime": copy.deepcopy(mod.REPAIR04_RUNTIME_CONTRACT),
    }
    inputs = {
        "rfq_resource_contract": binding,
        "registered_rfq_query_sha256": mod.sha256(execution_entry),
    }
    args = type("Args", (), {
        "memory_limit": "46GB", "max_temp_size": "70GB", "threads": 4,
        "min_free_gib": 100.0, "clob_max_per_root": 50,
        "resume": False, "keep_scratch": False,
    })()
    return run_dir, inputs, args, execution_entry


@pytest.mark.parametrize(
    "section,key,value,message",
    [
        ("current_runtime", "threads", 8, "resource contract content mismatch"),
        ("current_runtime", "max_temp_size", "120GB", "resource contract content mismatch"),
        ("disk_safety", "failed_scratch_deletion_allowed", True,
         "resource contract content mismatch"),
        ("w09", "replacement_instance_allowed", True,
         "resource contract content mismatch"),
    ],
)
def test_repair04_coordinated_contract_rewrites_fail_closed(
    tmp_path, section, key, value, message
):
    run_dir = tmp_path / "repair04-contract-mutation"
    run_dir.mkdir()
    applied_at = "2026-07-15T18:00:00Z"
    repair04 = {
        "failed_state_sha256": "1" * 64,
        "failed_resource_receipt_sha256": "2" * 64,
        "failed_input_identity_sha256": "3" * 64,
        "failed_scratch_receipt_sha256": "4" * 64,
        "resource_contract_path": mod.REPAIR04_RESOURCE_CONTRACT_PATH,
        "authority_basis_path": mod.REPAIR04_AUTHORITY_BASIS,
    }
    contract = mod._expected_repair04_resource_contract(
        run_dir.name, applied_at, repair04
    )
    contract[section][key] = value
    write_json(run_dir / mod.REPAIR04_RESOURCE_CONTRACT_PATH, contract)
    repair04["resource_contract_sha256"] = mod.sha256(
        run_dir / mod.REPAIR04_RESOURCE_CONTRACT_PATH
    )
    authority = {
        "schema_version": mod.REPAIR04_AUTHORITY_SCHEMA,
        "run_id": run_dir.name,
        "recorded_at_utc": applied_at,
        "mission_sha256": mod.EXPECTED_MISSION_SHA256,
        "authority_class": mod.REPAIR04_AUTHORITY_CLASS,
        "permitted_change": "EXISTING_W09_EXECUTION_RESOURCE_RETUNE_ONLY",
        "resource_contract_path": mod.REPAIR04_RESOURCE_CONTRACT_PATH,
        "resource_contract_sha256": repair04["resource_contract_sha256"],
        "existing_w09_instance_id": mod.EXPECTED_INSTANCE,
        "new_instance_spend_authorized": False,
        "instance_resize_authorized": False,
        "data_selection_change": "NONE",
        "parser_contract_change": "NONE",
        "query_semantics_change": "NONE",
        "quarantine_change": "NONE",
        "hypothesis_design_change": "NONE",
        "dependent_rfq_result_opened": False,
    }
    write_json(run_dir / mod.REPAIR04_AUTHORITY_BASIS, authority)
    repair04["authority_basis_sha256"] = mod.sha256(
        run_dir / mod.REPAIR04_AUTHORITY_BASIS
    )
    with pytest.raises(mod.RFQStageError, match=message):
        mod._validate_repair04_contract_artifacts(run_dir, repair04, applied_at)


def test_repair04_trial_suffix_matches_registration_producer_exactly():
    applied_at = "2026-07-15T18:00:00Z"
    previous_repository = {
        "execution_commit": "1" * 40,
        "source_manifest_sha256": "2" * 64,
        "source_sha256s_sha256": "3" * 64,
        "query_set_sha256": "4" * 64,
    }
    current_identity = {
        "execution_commit": "5" * 40,
        "source_manifest_sha256": "6" * 64,
        "source_sha256s_sha256": "7" * 64,
        "query_set_sha256": "8" * 64,
    }
    evidence = {
        "failed_state_path": mod.REPAIR04_FAILED_STATE_ARCHIVE,
        "failed_state_sha256": "9" * 64,
        "failed_resource_receipt_path": mod.REPAIR04_FAILED_RESOURCE_ARCHIVE,
        "failed_resource_receipt_sha256": "a" * 64,
        "failed_scratch_receipt_path": mod.REPAIR04_FAILED_SCRATCH_ARCHIVE,
        "failed_scratch_receipt_sha256": "b" * 64,
        "failed_input_identity_path": mod.REPAIR04_FAILED_INPUT_ARCHIVE,
        "failed_input_identity_sha256": "c" * 64,
        "resource_contract_path": mod.REPAIR04_RESOURCE_CONTRACT_PATH,
        "resource_contract_sha256": "d" * 64,
        "authority_basis_path": mod.REPAIR04_AUTHORITY_BASIS,
        "authority_basis_sha256": "e" * 64,
    }
    repair04 = {
        **evidence,
        "failed_input_fingerprint": repair04_producer.SELECTION_FINGERPRINT,
        "previous_selection_fingerprint_sha256": (
            repair04_producer.SELECTION_FINGERPRINT
        ),
        "current_selection_fingerprint_sha256": (
            repair04_producer.SELECTION_FINGERPRINT
        ),
    }
    coverage = {"status": "PARTIAL_OBJECT_COVERAGE_QUARANTINED"}
    cumulative = [{"key": "raw_rfq/quarantined"}]
    resource = {
        "wall_seconds": mod.REPAIR04_APPROVED_RESOURCE_WALL_SECONDS,
        "peak_process_tree_rss_kib_polled": (
            mod.REPAIR04_APPROVED_RESOURCE_PEAK_RSS_KIB
        ),
        "cumulative_children_max_rss_kib": 40_429_620,
        "peak_temp_bytes_polled": mod.REPAIR04_APPROVED_RESOURCE_PEAK_TEMP_BYTES,
        "minimum_disk_free_bytes_polled": (
            mod.REPAIR04_APPROVED_RESOURCE_MIN_FREE_BYTES
        ),
    }
    produced = list(repair04_producer.make_trial_records(
        applied_at,
        previous_repository,
        current_identity,
        evidence,
        coverage,
        cumulative,
        resource,
    ))
    consumed = mod._expected_repair04_trial_rows(
        applied_at,
        previous_repository,
        current_identity,
        repair04,
        coverage,
        cumulative,
        resource,
    )
    assert consumed == produced


def test_repair04_registration_product_is_accepted_by_consumer(
    tmp_path, monkeypatch
):
    helper_path = Path(__file__).with_name("test_repair04_registration.py")
    helper_spec = importlib.util.spec_from_file_location(
        "repair04_registration_fixture_for_consumer", helper_path
    )
    helpers = importlib.util.module_from_spec(helper_spec)
    assert helper_spec.loader is not None
    helper_spec.loader.exec_module(helpers)
    fixture = helpers.make_transaction_fixture(tmp_path, monkeypatch)
    assert helpers.invoke(fixture)["status"] == "REGISTRATION_REPAIR04_REFROZEN"

    run_dir = fixture["run"]
    manifest = json.loads((run_dir / "RUN_MANIFEST.json").read_text())
    repair03 = manifest["data_integrity_repairs"][2]
    repair04 = manifest["data_integrity_repairs"][3]
    coverage = repair04["coverage"]
    base_result = {
        "objects": coverage["full_unique_objects"],
        "logical_manifest_bindings": coverage[
            "full_logical_manifest_bindings"
        ],
        "bytes": coverage["full_unique_bytes"],
        "path_size_fingerprint_sha256": coverage["full_object_set_sha256"],
        "coverage_status": coverage["status"],
        "full_object_coverage": False,
        "unique_objects_total": coverage["full_unique_objects"],
        "unique_bytes_total": coverage["full_unique_bytes"],
        "consumed_unique_objects": coverage["retained_unique_objects"],
        "consumed_logical_bindings": coverage[
            "retained_logical_manifest_bindings"
        ],
        "consumed_bytes": coverage["retained_bytes"],
        "consumed_object_set_sha256": coverage["retained_object_set_sha256"],
        "quarantined_unique_objects": coverage["quarantined_unique_objects"],
        "quarantined_logical_bindings": coverage[
            "quarantined_logical_manifest_bindings"
        ],
        "quarantined_bytes": coverage["quarantined_bytes"],
        "quarantined_object_set_sha256": coverage[
            "quarantined_object_set_sha256"
        ],
        "selection_fingerprint_sha256": coverage[
            "retained_selection_fingerprint_sha256"
        ],
        "repair_chain": [
            {"repair_id": f"repair-0{index}"} for index in range(1, 4)
        ],
        "failed_attempt_bindings": [
            {"repair_id": f"repair-0{index}"} for index in range(1, 4)
        ],
        "inner_payload_parser_contract": {
            "path": repair03["parser_contract_path"],
            "sha256": repair03["parser_contract_sha256"],
            "schema_version": mod.REPAIR03_PARSER_SCHEMA,
        },
        "registered_rfq_query_sha256": repair03[
            "registered_rfq_query_sha256"
        ],
        "paths": [],
        "objects_detail": [],
    }
    inputs = {
        "objects": coverage["full_unique_objects"],
        "logical_manifest_bindings": coverage[
            "full_logical_manifest_bindings"
        ],
        "bytes": coverage["full_unique_bytes"],
        "path_size_fingerprint_sha256": coverage["full_object_set_sha256"],
    }
    monkeypatch.setattr(
        mod,
        "_apply_repair03_parser_contract",
        lambda *_args, **_kwargs: copy.deepcopy(base_result),
    )
    observed_cycle1_sources = []

    def validate_failed_boundary(
        _run_dir, _repair04, observed_base_result, cycle1_duckdb_binding
    ):
        # Production discover/quarantine output has this exact shape: Cycle-1 is
        # registered evidence, not a property synthesized onto base_result.
        assert "cycle1_duckdb_binding" not in observed_base_result
        observed_cycle1_sources.append(copy.deepcopy(cycle1_duckdb_binding))
        return {"resource": copy.deepcopy(fixture["resource"])}

    monkeypatch.setattr(
        mod, "_validate_repair04_failed_boundary", validate_failed_boundary
    )
    failed = repair04["failed_attempt"]
    monkeypatch.setattr(
        mod, "REPAIR04_APPROVED_FAILED_SCRATCH_SHA256",
        failed["preserved_scratch_sha256"],
    )
    monkeypatch.setattr(
        mod, "REPAIR04_APPROVED_FAILED_SCRATCH_BYTES",
        failed["preserved_scratch_bytes"],
    )
    monkeypatch.setattr(
        mod, "REPAIR04_APPROVED_FAILED_SCRATCH_MTIME",
        failed["preserved_scratch_mtime_utc"],
    )
    monkeypatch.setattr(
        mod, "REPAIR04_APPROVED_FAILED_SCRATCH_INODE",
        failed["preserved_scratch_original_inode"],
    )

    pre_evidence = {
        relative: (
            (run_dir / relative).read_bytes()
            if (run_dir / relative).is_file() else None
        )
        for relative in (
            "REPORT/tables/RFQ_FULL_STAGE_STATE.json",
            "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json",
            "cache/rfq_full_scratch.duckdb",
        )
    }
    result = mod._apply_repair04_resource_contract(run_dir, manifest, inputs)
    assert pre_evidence == {
        relative: (
            (run_dir / relative).read_bytes()
            if (run_dir / relative).is_file() else None
        )
        for relative in pre_evidence
    }
    assert observed_cycle1_sources == [repair03["cycle1_duckdb_binding"]]
    assert [row["repair_id"] for row in result["repair_chain"]] == [
        "repair-01", "repair-02", "repair-03", "repair-04",
    ]
    assert result["rfq_resource_contract"] == {
        "path": mod.REPAIR04_RESOURCE_CONTRACT_PATH,
        "sha256": repair04["resource_contract_sha256"],
        "schema_version": mod.REPAIR04_RESOURCE_SCHEMA,
        "current_runtime": mod.REPAIR04_RUNTIME_CONTRACT,
    }


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("memory_limit", "40GB"),
        ("max_temp_size", "120GB"),
        ("threads", 8),
        ("min_free_gib", 120.0),
        ("clob_max_per_root", 51),
        ("resume", True),
        ("keep_scratch", True),
    ],
)
def test_repair04_runtime_contract_is_enforced_before_attempt_mutation(
    tmp_path, monkeypatch, field, invalid
):
    run_dir, inputs, args, _ = repair04_runtime_enforcement_fixture(
        tmp_path, monkeypatch
    )
    mod._enforce_registered_resource_contract(args, inputs, run_dir)
    setattr(args, field, invalid)
    with pytest.raises(mod.RFQStageError, match="runtime arguments differ"):
        mod._enforce_registered_resource_contract(args, inputs, run_dir)


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("extra", True),
        ("path", "DATA_INTEGRITY/forged.json"),
        ("sha256", "not-a-sha"),
        ("schema_version", "rfq-execution-resource-contract-v2"),
        ("current_runtime", {"threads": 4}),
    ],
)
def test_repair04_runtime_binding_shape_fails_closed(
    tmp_path, monkeypatch, mutation, value
):
    run_dir, inputs, args, _ = repair04_runtime_enforcement_fixture(
        tmp_path, monkeypatch
    )
    binding = inputs["rfq_resource_contract"]
    binding[mutation] = value
    with pytest.raises(mod.RFQStageError, match="registered resource contract"):
        mod._enforce_registered_resource_contract(args, inputs, run_dir)


@pytest.mark.parametrize(
    "mutation",
    ["legacy_source_path", "query_bytes", "query_symlink", "registered_sha"],
)
def test_repair04_execution_entry_must_be_registered_frozen_query(
    tmp_path, monkeypatch, mutation
):
    run_dir, inputs, args, execution_entry = repair04_runtime_enforcement_fixture(
        tmp_path, monkeypatch
    )
    if mutation == "legacy_source_path":
        legacy = run_dir / "source/rfq_full_stage.py"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(execution_entry.read_bytes())
        monkeypatch.setattr(mod, "__file__", str(legacy))
    elif mutation == "query_bytes":
        execution_entry.write_bytes(b"# unregistered repair-04 query\n")
    elif mutation == "query_symlink":
        payload = execution_entry.read_bytes()
        execution_entry.unlink()
        target = run_dir / "unregistered-rfq-query.py"
        target.write_bytes(payload)
        execution_entry.symlink_to(target)
    else:
        inputs["registered_rfq_query_sha256"] = "0" * 64
    with pytest.raises(mod.RFQStageError, match="execution entry"):
        mod._enforce_registered_resource_contract(args, inputs, run_dir)


def test_resource_gate_is_noop_without_repair04_contract(tmp_path):
    args = type("Args", (), {})()
    mod._enforce_registered_resource_contract(args, {}, tmp_path)


def repair05_consumer_product_fixture(tmp_path, monkeypatch):
    fixture = repair05_test_helpers.make_transaction_fixture(tmp_path, monkeypatch)
    assert repair05_test_helpers.invoke(fixture)["status"] == (
        "REGISTRATION_REPAIR05_REFROZEN"
    )
    run_dir = fixture["run"]
    manifest = json.loads((run_dir / "RUN_MANIFEST.json").read_text())
    repair04 = manifest["data_integrity_repairs"][3]
    repair05 = manifest["data_integrity_repairs"][4]
    selection = repair05["current_selection_fingerprint_sha256"]
    base_result = {
        "selection_fingerprint_sha256": selection,
        "repair_chain": [
            {"repair_id": f"repair-0{index}"} for index in range(1, 5)
        ],
        "failed_attempt_bindings": [
            {"repair_id": f"repair-0{index}"} for index in range(1, 5)
        ],
        "rfq_resource_contract": {
            "path": repair04["resource_contract_path"],
            "sha256": repair04["resource_contract_sha256"],
            "schema_version": mod.REPAIR04_RESOURCE_SCHEMA,
            "current_runtime": copy.deepcopy(mod.REPAIR04_RUNTIME_CONTRACT),
        },
        "inner_payload_parser_contract": {
            "path": repair04["parser_contract_path"],
            "sha256": repair04["parser_contract_sha256"],
            "schema_version": mod.REPAIR03_PARSER_SCHEMA,
        },
        "registered_rfq_query_sha256": repair04[
            "registered_rfq_query_sha256"
        ],
    }
    monkeypatch.setattr(
        mod, "_apply_repair04_resource_contract",
        lambda *_args, **_kwargs: copy.deepcopy(base_result),
    )
    monkeypatch.setattr(
        mod, "_validate_repair05_failure_evidence",
        lambda *_args, **_kwargs: {
            "resource": copy.deepcopy(fixture["resource"]),
            "cycle1_duckdb_binding": copy.deepcopy(
                repair05["cycle1_duckdb_binding"]
            ),
        },
    )
    return {
        **fixture,
        "run_dir": run_dir,
        "manifest": manifest,
        "repair04": repair04,
        "repair05": repair05,
        "inputs": {
            "objects": repair05["coverage"]["full_unique_objects"],
            "logical_manifest_bindings": repair05["coverage"][
                "full_logical_manifest_bindings"
            ],
            "bytes": repair05["coverage"]["full_unique_bytes"],
            "path_size_fingerprint_sha256": repair05["coverage"][
                "full_object_set_sha256"
            ],
        },
    }


def _rebind_repair05_record(fixture):
    run_dir = fixture["run_dir"]
    manifest_path = run_dir / "RUN_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text())
    record = manifest["data_integrity_repairs"][4]
    journal_path = run_dir / mod.REPAIR05_TRANSACTION_JOURNAL
    record["transaction_journal_sha256"] = mod.sha256(journal_path)
    receipt_path = run_dir / mod.REPAIR05_REGISTRATION
    receipt = {
        key: value for key, value in record.items()
        if key not in {"repair_receipt_path", "repair_receipt_sha256"}
    }
    write_json(receipt_path, receipt)
    record["repair_receipt_sha256"] = mod.sha256(receipt_path)
    write_json(manifest_path, manifest)
    write_json(
        run_dir / mod.REPAIR05_ROOT / "post_repair/RUN_MANIFEST.json",
        manifest,
    )
    fixture["manifest"] = manifest
    fixture["repair05"] = record


def test_repair05_wiring_contract_matches_registration_producer_exactly():
    evidence = {
        "blocker_path": mod.REPAIR05_BLOCKER_ARCHIVE,
        "blocker_sha256": mod.REPAIR05_BLOCKER_SHA256,
        "failed_resource_receipt_path": mod.REPAIR05_FAILED_RESOURCE_ARCHIVE,
        "failed_resource_receipt_sha256": mod.REPAIR05_FAILED_RESOURCE_SHA256,
        "unchanged_state_sha256": mod.REPAIR04_APPROVED_FAILED_STATE_SHA256,
        "unchanged_input_identity_sha256": (
            mod.REPAIR04_APPROVED_FAILED_INPUT_SHA256
        ),
        "parent_registered_rfq_query_sha256": (
            repair05_producer.PARENT_RFQ_QUERY_SHA256
        ),
        "parent_resource_contract_sha256": (
            repair05_producer.PARENT_RESOURCE_CONTRACT_SHA256
        ),
        "cycle1_binding_path": mod.REPAIR05_CYCLE1_BINDING_ARCHIVE,
        "cycle1_binding_sha256": "b" * 64,
        "registered_cycle1_duckdb_binding": {
            "active_path": mod.CYCLE1_DUCKDB_BINDING,
            "active_sha256": "b" * 64,
        },
    }
    current_query_sha = "a" * 64
    produced = repair05_producer.make_wiring_contract(
        "run", "2026-07-15T19:00:00Z", evidence, current_query_sha
    )
    consumed = mod._expected_repair05_wiring_contract(
        "run", "2026-07-15T19:00:00Z", evidence, current_query_sha
    )
    assert consumed == produced


def test_repair05_registration_product_is_accepted_by_strict_consumer(
    tmp_path, monkeypatch
):
    fixture = repair05_consumer_product_fixture(tmp_path, monkeypatch)
    result = mod._apply_repair05_consumer_wiring(
        fixture["run_dir"], fixture["manifest"], fixture["inputs"]
    )
    assert [row["repair_id"] for row in result["repair_chain"]] == [
        "repair-01", "repair-02", "repair-03", "repair-04", "repair-05",
    ]
    assert result["consumer_wiring_contract"] == {
        "path": mod.REPAIR05_WIRING_CONTRACT,
        "sha256": fixture["repair05"]["wiring_contract_sha256"],
        "schema_version": mod.REPAIR05_WIRING_SCHEMA,
    }


@pytest.mark.parametrize("mutation", ["missing", "tampered"])
def test_repair05_consumer_rejects_missing_or_tampered_journal(
    tmp_path, monkeypatch, mutation
):
    fixture = repair05_consumer_product_fixture(tmp_path, monkeypatch)
    journal = fixture["run_dir"] / mod.REPAIR05_TRANSACTION_JOURNAL
    if mutation == "missing":
        journal.unlink()
    else:
        journal.write_bytes(journal.read_bytes() + b" ")
    with pytest.raises(mod.RFQStageError, match="transaction journal"):
        mod._apply_repair05_consumer_wiring(
            fixture["run_dir"], fixture["manifest"], fixture["inputs"]
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("original_sha256", "0" * 64),
        ("replacement_staging_path", "DATA_INTEGRITY/forged"),
        ("displaced_path", "DATA_INTEGRITY/forged-displaced"),
    ],
)
def test_repair05_consumer_rejects_transaction_mutation_mismatch(
    tmp_path, monkeypatch, field, value
):
    fixture = repair05_consumer_product_fixture(tmp_path, monkeypatch)
    journal_path = fixture["run_dir"] / mod.REPAIR05_TRANSACTION_JOURNAL
    journal = json.loads(journal_path.read_text())
    journal["active_mutations"][0][field] = value
    write_json(journal_path, journal)
    _rebind_repair05_record(fixture)
    with pytest.raises(mod.RFQStageError, match="transaction mutation mismatch"):
        mod._apply_repair05_consumer_wiring(
            fixture["run_dir"], fixture["manifest"], fixture["inputs"]
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("replacement_sha256", "0" * 64),
        ("replacement_staging_path", "DATA_INTEGRITY/forged-manifest"),
        ("displaced_path", "DATA_INTEGRITY/forged-displaced-manifest"),
    ],
)
def test_repair05_consumer_rejects_manifest_mutation_mismatch(
    tmp_path, monkeypatch, field, value
):
    fixture = repair05_consumer_product_fixture(tmp_path, monkeypatch)
    journal_path = fixture["run_dir"] / mod.REPAIR05_TRANSACTION_JOURNAL
    journal = json.loads(journal_path.read_text())
    journal["manifest_mutation"][field] = value
    write_json(journal_path, journal)
    _rebind_repair05_record(fixture)
    with pytest.raises(
        mod.RFQStageError, match="manifest transaction mutation mismatch"
    ):
        mod._apply_repair05_consumer_wiring(
            fixture["run_dir"], fixture["manifest"], fixture["inputs"]
        )


def test_repair05_consumer_rejects_committed_displaced_path(
    tmp_path, monkeypatch
):
    fixture = repair05_consumer_product_fixture(tmp_path, monkeypatch)
    journal = json.loads(
        (fixture["run_dir"] / mod.REPAIR05_TRANSACTION_JOURNAL).read_text()
    )
    displaced = fixture["run_dir"] / journal["active_mutations"][0][
        "displaced_path"
    ]
    displaced.parent.mkdir(parents=True, exist_ok=True)
    displaced.write_bytes(b"stale displaced bytes")
    with pytest.raises(
        mod.RFQStageError, match="transaction journal|transaction mutation mismatch"
    ):
        mod._apply_repair05_consumer_wiring(
            fixture["run_dir"], fixture["manifest"], fixture["inputs"]
        )


def test_repair05_consumer_rejects_staging_active_byte_mismatch(
    tmp_path, monkeypatch
):
    fixture = repair05_consumer_product_fixture(tmp_path, monkeypatch)
    run_dir = fixture["run_dir"]
    journal_path = run_dir / mod.REPAIR05_TRANSACTION_JOURNAL
    journal = json.loads(journal_path.read_text())
    mutation = next(
        row for row in journal["active_mutations"]
        if row["path"] == "TRIAL_REGISTRY.jsonl"
    )
    staging = run_dir / mutation["replacement_staging_path"]
    staging.write_bytes(staging.read_bytes() + b"forged-staging")
    mutation["replacement_sha256"] = mod.sha256(staging)
    write_json(journal_path, journal)
    _rebind_repair05_record(fixture)
    with pytest.raises(mod.RFQStageError, match="transaction mutation mismatch"):
        mod._apply_repair05_consumer_wiring(
            fixture["run_dir"], fixture["manifest"], fixture["inputs"]
        )


def test_repair05_consumer_rejects_staged_active_manifest_byte_mismatch(
    tmp_path, monkeypatch
):
    fixture = repair05_consumer_product_fixture(tmp_path, monkeypatch)
    staged = (
        fixture["run_dir"] / mod.REPAIR05_ROOT
        / "post_repair/RUN_MANIFEST.json"
    )
    value = json.loads(staged.read_text())
    value["forged_staging_only"] = True
    write_json(staged, value)
    with pytest.raises(
        mod.RFQStageError, match="manifest transaction mutation mismatch"
    ):
        mod._apply_repair05_consumer_wiring(
            fixture["run_dir"], fixture["manifest"], fixture["inputs"]
        )


def test_repair05_consumer_rejects_repository_scalar_mismatch(
    tmp_path, monkeypatch
):
    fixture = repair05_consumer_product_fixture(tmp_path, monkeypatch)
    fixture["manifest"]["repository"]["previous_source_manifest_sha256"] = (
        "0" * 64
    )
    write_json(
        fixture["run_dir"] / "RUN_MANIFEST.json", fixture["manifest"]
    )
    write_json(
        fixture["run_dir"] / mod.REPAIR05_ROOT
        / "post_repair/RUN_MANIFEST.json",
        fixture["manifest"],
    )
    with pytest.raises(mod.RFQStageError, match="repository transfer mismatch"):
        mod._apply_repair05_consumer_wiring(
            fixture["run_dir"], fixture["manifest"], fixture["inputs"]
        )


@pytest.mark.parametrize(
    "mutation",
    ["failed_attempt", "expected_success", "core_disposition", "quarantine_policy"],
)
def test_repair05_consumer_rejects_nested_governance_mismatch(
    tmp_path, monkeypatch, mutation
):
    fixture = repair05_consumer_product_fixture(tmp_path, monkeypatch)
    manifest_path = fixture["run_dir"] / "RUN_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text())
    record = manifest["data_integrity_repairs"][4]
    if mutation == "failed_attempt":
        record["failed_attempt"]["error"] = "forged"
    elif mutation == "expected_success":
        record["expected_success_resource"]["label"] = "forged"
    elif mutation == "core_disposition":
        record["core_result_disposition"] = "RECOMPUTED"
    else:
        record["quarantine_policy"] = "FORGED"
    write_json(manifest_path, manifest)
    _rebind_repair05_record(fixture)
    with pytest.raises(
        mod.RFQStageError,
        match="identity/governance|inherited research semantics",
    ):
        mod._apply_repair05_consumer_wiring(
            fixture["run_dir"], fixture["manifest"], fixture["inputs"]
        )


def test_repair05_consumer_rejects_exact_trial_suffix_mismatch(
    tmp_path, monkeypatch
):
    fixture = repair05_consumer_product_fixture(tmp_path, monkeypatch)
    run_dir = fixture["run_dir"]
    pre_registry = (
        run_dir / mod.REPAIR05_PRE_ROOT / "TRIAL_REGISTRY.jsonl"
    ).read_bytes()
    registry_path = run_dir / "TRIAL_REGISTRY.jsonl"
    suffix = registry_path.read_bytes()[len(pre_registry):]
    rows = mod._read_registry_rows(suffix, "repair-05 test suffix")
    rows[0]["failure_class"] = "FORGED_FAILURE_CLASS"
    registry = pre_registry + b"".join(
        repair05_test_helpers.repair.base.registry_line(row) for row in rows
    )
    registry_path.write_bytes(registry)
    (
        run_dir / mod.REPAIR05_ROOT / "post_repair/TRIAL_REGISTRY.jsonl"
    ).write_bytes(registry)

    journal_path = run_dir / mod.REPAIR05_TRANSACTION_JOURNAL
    journal = json.loads(journal_path.read_text())
    registry_mutation = next(
        row for row in journal["active_mutations"]
        if row["path"] == "TRIAL_REGISTRY.jsonl"
    )
    registry_mutation["replacement_sha256"] = mod.sha256(registry_path)
    write_json(journal_path, journal)

    manifest_path = run_dir / "RUN_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text())
    record = manifest["data_integrity_repairs"][4]
    record["trial_registry"]["current_sha256"] = mod.sha256(registry_path)
    record["trial_registry"]["current_bytes"] = len(registry)
    write_json(manifest_path, manifest)
    _rebind_repair05_record(fixture)
    with pytest.raises(mod.RFQStageError, match="registry suffix mismatch"):
        mod._apply_repair05_consumer_wiring(
            fixture["run_dir"], fixture["manifest"], fixture["inputs"]
        )


@pytest.mark.parametrize("mutation", ["scratch", "wal", "cache_symlink"])
def test_repair05_fresh_scratch_gate_rejects_dangling_or_escaped_paths(
    tmp_path, mutation
):
    run_dir = tmp_path / "repair05-scratch-boundary"
    cache = run_dir / "cache"
    cache.mkdir(parents=True)
    if mutation == "scratch":
        (cache / "rfq_full_scratch.duckdb").symlink_to(
            tmp_path / "missing-scratch-target"
        )
    elif mutation == "wal":
        (cache / "rfq_full_scratch.duckdb.wal").symlink_to(
            tmp_path / "missing-wal-target"
        )
    else:
        outside = tmp_path / "outside-cache"
        outside.mkdir()
        cache.rmdir()
        cache.symlink_to(outside, target_is_directory=True)
    with pytest.raises(mod.RFQStageError, match="scratch|WAL"):
        mod._validate_repair05_fresh_scratch_absence(run_dir)


def test_repair05_wiring_runtime_gate_is_before_evidence_mutation(tmp_path):
    run_dir = tmp_path / "repair05-wiring-gate"
    contract = run_dir / mod.REPAIR05_WIRING_CONTRACT
    contract.parent.mkdir(parents=True)
    contract.write_bytes(b'{"schema_version":"synthetic"}\n')
    binding = {
        "path": mod.REPAIR05_WIRING_CONTRACT,
        "sha256": mod.sha256(contract),
        "schema_version": mod.REPAIR05_WIRING_SCHEMA,
    }
    mod._enforce_registered_consumer_wiring_contract(
        {"consumer_wiring_contract": binding}, run_dir
    )
    before = {
        relative: (run_dir / relative).exists()
        for relative in (
            "REPORT/tables/RFQ_FULL_STAGE_STATE.json",
            "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json",
            "cache/rfq_full_scratch.duckdb",
        )
    }
    contract.write_bytes(b'{"schema_version":"tampered"}\n')
    with pytest.raises(mod.RFQStageError, match="wiring contract changed"):
        mod._enforce_registered_consumer_wiring_contract(
            {"consumer_wiring_contract": binding}, run_dir
        )
    assert before == {
        relative: (run_dir / relative).exists() for relative in before
    }


def test_apply_object_quarantine_dispatches_exact_repair05_chain(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "repair05-dispatch"
    (run_dir / mod.QUARANTINE_DECLARATION_02).parent.mkdir(parents=True)
    (run_dir / mod.QUARANTINE_DECLARATION_02).write_bytes(b"{}\n")
    (run_dir / mod.MALFORMED_OBJECT_RECEIPT_02).write_bytes(b"{}\n")
    manifest = {
        "data_integrity_repairs": [
            {"repair_id": f"repair-0{index}"} for index in range(1, 6)
        ]
    }
    sentinel = {"repair05": "validated"}
    monkeypatch.setattr(
        mod,
        "_apply_repair05_consumer_wiring",
        lambda observed_run, observed_manifest, observed_inputs: (
            sentinel
            if (observed_run, observed_manifest, observed_inputs)
            == (run_dir, manifest, {"inputs": True})
            else None
        ),
    )
    assert mod.apply_object_quarantine(
        run_dir, manifest, {"inputs": True}
    ) == sentinel


def test_apply_object_quarantine_dispatches_exact_repair06_chain(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "repair06-dispatch"
    (run_dir / mod.QUARANTINE_DECLARATION_02).parent.mkdir(parents=True)
    (run_dir / mod.QUARANTINE_DECLARATION_02).write_bytes(b"{}\n")
    (run_dir / mod.MALFORMED_OBJECT_RECEIPT_02).write_bytes(b"{}\n")
    manifest = {
        "data_integrity_repairs": [
            {"repair_id": f"repair-0{index}"} for index in range(1, 7)
        ]
    }
    sentinel = {"repair06": "validated"}
    observed = {}

    def apply06(observed_run, observed_manifest, observed_inputs, *, active_root=None):
        observed.update({
            "run": observed_run,
            "manifest": observed_manifest,
            "inputs": observed_inputs,
            "active_root": active_root,
        })
        return sentinel

    monkeypatch.setattr(mod, "_apply_repair06_status_wiring", apply06)
    overlay = run_dir / mod.REPAIR06_ROOT / "post_repair"
    assert mod.apply_object_quarantine(
        run_dir,
        manifest,
        {"inputs": True},
        _active_root=overlay,
    ) == sentinel
    assert observed == {
        "run": run_dir,
        "manifest": manifest,
        "inputs": {"inputs": True},
        "active_root": overlay,
    }


def test_apply_object_quarantine_dispatches_exact_repair04_chain(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "repair04-dispatch"
    run_dir.mkdir()
    (run_dir / mod.QUARANTINE_DECLARATION_02).parent.mkdir(parents=True)
    (run_dir / mod.QUARANTINE_DECLARATION_02).write_bytes(b"{}\n")
    (run_dir / mod.MALFORMED_OBJECT_RECEIPT_02).write_bytes(b"{}\n")
    manifest = {
        "data_integrity_repairs": [
            {"repair_id": f"repair-0{index}"} for index in range(1, 5)
        ]
    }
    sentinel = {"repair04": "validated"}
    monkeypatch.setattr(
        mod,
        "_apply_repair04_resource_contract",
        lambda observed_run, observed_manifest, observed_inputs: (
            sentinel
            if (observed_run, observed_manifest, observed_inputs)
            == (run_dir, manifest, {"inputs": True})
            else None
        ),
    )
    assert mod.apply_object_quarantine(
        run_dir, manifest, {"inputs": True}
    ) == sentinel


def test_main_repair04_writes_v4_identity_and_resource_binding(
    tmp_path, monkeypatch
):
    fixture = repair03_main_failure_fixture(tmp_path, monkeypatch)
    fixture["manifest"]["data_integrity_repairs"].append(
        {"repair_id": "repair-04"}
    )
    binding = {
        "path": mod.REPAIR04_RESOURCE_CONTRACT_PATH,
        "sha256": "2" * 64,
        "schema_version": mod.REPAIR04_RESOURCE_SCHEMA,
        "current_runtime": copy.deepcopy(mod.REPAIR04_RUNTIME_CONTRACT),
    }
    execution_entry = fixture["run_dir"] / mod.REPAIR04_EXECUTION_QUERY
    execution_entry.parent.mkdir(parents=True)
    execution_entry.write_bytes(path.read_bytes())
    monkeypatch.setattr(mod, "__file__", str(execution_entry))
    fixture["inputs"].update({
        "repair_chain": [
            {"repair_id": "repair-03"}, {"repair_id": "repair-04"},
        ],
        "failed_attempt_bindings": [
            {"repair_id": "repair-03"}, {"repair_id": "repair-04"},
        ],
        "expected_success_resource": {
            "label": mod.REPAIR04_SUCCESS_RESOURCE_LABEL,
            "path": mod.REPAIR04_SUCCESS_RESOURCE_PATH,
        },
        "rfq_resource_contract": binding,
        "registered_rfq_query_sha256": mod.sha256(execution_entry),
    })
    monkeypatch.setattr(
        mod.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 2**50})(),
    )

    def fail_connect(path):
        Path(path).write_bytes(b"partial repair04 scratch")
        raise RuntimeError("synthetic repair04 connect failure")

    monkeypatch.setattr(duckdb, "connect", fail_connect)
    with pytest.raises(RuntimeError, match="repair04 connect failure"):
        mod.main([
            "--run-dir", str(fixture["run_dir"]),
            "--cache-root", str(tmp_path / "cache"),
            "--memory-limit", "46GB", "--max-temp-size", "70GB",
            "--threads", "4", "--min-free-gib", "100",
            "--clob-max-per-root", "50",
        ])
    identity = json.loads(fixture["input_path"].read_text())
    state = json.loads(fixture["state_path"].read_text())
    assert identity["schema"] == "rfq-full-input-identity-v4"
    assert identity["rfq_resource_contract"] == binding
    assert state["rfq_resource_contract"] == binding
    assert state["expected_success_resource"] == {
        "label": mod.REPAIR04_SUCCESS_RESOURCE_LABEL,
        "path": mod.REPAIR04_SUCCESS_RESOURCE_PATH,
    }
    assert state["status"] == "FAILED_DATA_INTEGRITY_REQUIRES_NEW_PREREGISTRATION"
    assert fixture["scratch"].read_bytes() == b"partial repair04 scratch"


def test_main_repair05_writes_v5_identity_and_both_contract_bindings(
    tmp_path, monkeypatch
):
    fixture = repair03_main_failure_fixture(tmp_path, monkeypatch)
    fixture["manifest"]["data_integrity_repairs"].extend([
        {"repair_id": "repair-04"}, {"repair_id": "repair-05"},
    ])
    execution_entry = fixture["run_dir"] / mod.REPAIR05_EXECUTION_QUERY
    execution_entry.parent.mkdir(parents=True)
    execution_entry.write_bytes(path.read_bytes())
    monkeypatch.setattr(mod, "__file__", str(execution_entry))
    resource_binding = {
        "path": mod.REPAIR04_RESOURCE_CONTRACT_PATH,
        "sha256": "2" * 64,
        "schema_version": mod.REPAIR04_RESOURCE_SCHEMA,
        "current_runtime": copy.deepcopy(mod.REPAIR04_RUNTIME_CONTRACT),
    }
    wiring_path = fixture["run_dir"] / mod.REPAIR05_WIRING_CONTRACT
    wiring_path.parent.mkdir(parents=True)
    wiring_path.write_bytes(b'{"schema_version":"synthetic-repair05"}\n')
    wiring_binding = {
        "path": mod.REPAIR05_WIRING_CONTRACT,
        "sha256": mod.sha256(wiring_path),
        "schema_version": mod.REPAIR05_WIRING_SCHEMA,
    }
    fixture["inputs"].update({
        "repair_chain": [
            {"repair_id": "repair-04"}, {"repair_id": "repair-05"},
        ],
        "failed_attempt_bindings": [
            {"repair_id": "repair-04"}, {"repair_id": "repair-05"},
        ],
        "expected_success_resource": {
            "label": mod.REPAIR05_SUCCESS_RESOURCE_LABEL,
            "path": mod.REPAIR05_SUCCESS_RESOURCE_PATH,
        },
        "rfq_resource_contract": resource_binding,
        "consumer_wiring_contract": wiring_binding,
        "registered_rfq_query_sha256": mod.sha256(execution_entry),
    })
    monkeypatch.setattr(
        mod.shutil, "disk_usage",
        lambda _path: type("Usage", (), {"free": 2**50})(),
    )

    def fail_connect(path):
        Path(path).write_bytes(b"partial repair05 scratch")
        raise RuntimeError("synthetic repair05 connect failure")

    monkeypatch.setattr(duckdb, "connect", fail_connect)
    with pytest.raises(RuntimeError, match="repair05 connect failure"):
        mod.main([
            "--run-dir", str(fixture["run_dir"]),
            "--cache-root", str(tmp_path / "cache"),
            "--memory-limit", "46GB", "--max-temp-size", "70GB",
            "--threads", "4", "--min-free-gib", "100",
            "--clob-max-per-root", "50",
        ])
    identity = json.loads(fixture["input_path"].read_text())
    state = json.loads(fixture["state_path"].read_text())
    assert identity["schema"] == "rfq-full-input-identity-v5"
    assert identity["rfq_resource_contract"] == resource_binding
    assert identity["consumer_wiring_contract"] == wiring_binding
    assert state["rfq_resource_contract"] == resource_binding
    assert state["consumer_wiring_contract"] == wiring_binding
    assert state["expected_success_resource"] == {
        "label": mod.REPAIR05_SUCCESS_RESOURCE_LABEL,
        "path": mod.REPAIR05_SUCCESS_RESOURCE_PATH,
    }


def _repair06_validate_run_fixture(tmp_path, monkeypatch):
    run_dir = tmp_path / "repair06-validate-run"
    (run_dir / "DATA_INTEGRITY/manifests").mkdir(parents=True)
    (run_dir / "REPORT/tables").mkdir(parents=True)
    (run_dir / "cache").mkdir(parents=True)
    attestation = {
        "instance_id": mod.EXPECTED_INSTANCE,
        "region": mod.EXPECTED_REGION,
        "role": mod.EXPECTED_ROLE,
        "instance_profile": mod.EXPECTED_ROLE,
        "architecture": "aarch64",
        "w09_run_inhibitor_present": True,
        "w09_run_inhibitor_is_ancestor": True,
        "static_credentials_present": False,
        "trading_credentials_present": False,
        "ambient_aws_or_kalshi_variables": [],
        "static_credential_paths_present": [],
        "installation_sha256": mod.EXPECTED_W09_INSTALLATION_SHA256,
        "s3_access": "READ_ONLY_RESEARCH_PREFIX",
        "duckdb": mod.EXPECTED_DUCKDB,
    }
    attestation_path = run_dir / "DATA_INTEGRITY/W09_ATTESTATION.json"
    write_json(attestation_path, attestation)
    expected_manifests = {}
    selected = []
    for index, release_id in enumerate(mod.RELEASE_IDS):
        manifest_copy = run_dir / "DATA_INTEGRITY/manifests" / f"{release_id}.json"
        manifest_copy.write_bytes(f"synthetic-manifest-{index}\n".encode())
        digest = mod.sha256(manifest_copy)
        expected_manifests[release_id] = digest
        selected.append({
            "release_id": release_id,
            "evidence_tier": mod.EVIDENCE,
            "include": "EXPLORATORY_ONLY",
            "manifest_sha256": digest,
        })
    monkeypatch.setattr(mod, "EXPECTED_MANIFEST_SHA256", expected_manifests)
    manifest = {
        "run_id": run_dir.name,
        "mode": "EXPLORATORY_AUTORESEARCH",
        "mission": {"sha256": mod.EXPECTED_MISSION_SHA256},
        "explicit_degraded_admission": True,
        "analysis_started": True,
        "status": mod.REPAIR06_STATUS,
        "registration_state": mod.REPAIR06_REGISTRATION_STATE,
        "gates": {
            "gate_a": {"status": "PASS_SYNTHETIC"},
            "gate_b": {
                "status": "PASS_SYNTHETIC",
                "attestation_sha256": mod.sha256(attestation_path),
            },
            "gate_c": {
                "status": "PASS_SYNTHETIC",
                "mode2_authorized": False,
            },
        },
        "selected_releases": selected,
        "data_integrity_repairs": [
            {"repair_id": f"repair-0{index}"} for index in range(1, 7)
        ],
    }
    write_json(run_dir / "RUN_MANIFEST.json", manifest)
    return run_dir, manifest


def test_repair06_contract_and_authority_match_registration_producer_exactly():
    parent = {
        "repair_receipt_path": mod.REPAIR05_REGISTRATION,
        "repair_receipt_sha256": repair06_producer.PARENT_RECEIPT_SHA256,
        "transaction_journal_path": mod.REPAIR05_TRANSACTION_JOURNAL,
        "transaction_journal_sha256": repair06_producer.PARENT_JOURNAL_SHA256,
        "wiring_contract_path": mod.REPAIR05_WIRING_CONTRACT,
        "wiring_contract_sha256": repair06_producer.PARENT_WIRING_SHA256,
        "authority_basis_path": mod.REPAIR05_AUTHORITY_BASIS,
        "authority_basis_sha256": repair06_producer.PARENT_AUTHORITY_SHA256,
        "core_result_artifacts": [{"path": "core", "sha256": "0" * 64}],
        "coverage": {"status": "PARTIAL_OBJECT_COVERAGE_QUARANTINED"},
        "cycle1_duckdb_binding": {"active_sha256": "1" * 64},
        "parser_contract": {"schema_version": mod.REPAIR03_PARSER_SCHEMA},
        "resource_contract": {"schema_version": mod.REPAIR04_RESOURCE_SCHEMA},
    }
    run_id = "repair06-producer-parity"
    applied_at = "2026-07-15T20:00:00Z"
    produced_contract = repair06_producer.make_status_wiring_contract(
        run_id, applied_at, parent
    )
    consumed_contract = mod._expected_repair06_status_wiring_contract(
        run_id,
        applied_at,
        {},
        parent,
        repair06_producer.PARENT_MANIFEST_SHA256,
    )
    assert consumed_contract == produced_contract
    contract_sha = "a" * 64
    assert mod._expected_repair06_authority_basis(
        run_id, applied_at, contract_sha
    ) == repair06_producer.make_authority(
        run_id,
        applied_at,
        mod.REPAIR06_STATUS_WIRING_CONTRACT,
        contract_sha,
    )


def test_repair06_registration_product_is_accepted_by_strict_consumer(
    tmp_path, monkeypatch
):
    fixture = repair06_test_helpers.make_fixture(tmp_path, monkeypatch)
    assert repair06_test_helpers.invoke(fixture)["status"] == (
        "REGISTRATION_REPAIR06_REFROZEN"
    )
    run_dir = fixture["run"]
    manifest = json.loads((run_dir / "RUN_MANIFEST.json").read_text())
    parent = manifest["data_integrity_repairs"][4]
    record = manifest["data_integrity_repairs"][5]
    fixed = {
        "REPAIR06_PARENT_MANIFEST_SHA256": record["parent_manifest_sha256"],
        "REPAIR06_PARENT_RECEIPT_SHA256": record[
            "parent_repair_registration_sha256"
        ],
        "REPAIR06_PARENT_JOURNAL_SHA256": record[
            "parent_transaction_journal_sha256"
        ],
        "REPAIR06_PARENT_WIRING_SHA256": record[
            "parent_wiring_contract_sha256"
        ],
        "REPAIR06_PARENT_AUTHORITY_SHA256": record[
            "parent_authority_basis_sha256"
        ],
        "REPAIR06_BLOCKER_SHA256": record["blocker_sha256"],
        "REPAIR06_FAILED_RESOURCE_SHA256": record[
            "failed_resource_receipt_sha256"
        ],
        "REPAIR06_PARENT_RFQ_QUERY_SHA256": parent[
            "registered_rfq_query_sha256"
        ],
        "REPAIR04_APPROVED_FAILED_STATE_SHA256": record[
            "unchanged_state_sha256"
        ],
        "REPAIR04_APPROVED_FAILED_INPUT_SHA256": record[
            "unchanged_input_identity_sha256"
        ],
    }
    for name, value in fixed.items():
        monkeypatch.setattr(mod, name, value)
    base_result = {
        "selection_fingerprint_sha256": parent[
            "current_selection_fingerprint_sha256"
        ],
        "repair_chain": [
            {"repair_id": f"repair-0{index}"} for index in range(1, 6)
        ],
        "failed_attempt_bindings": [
            {"repair_id": f"repair-0{index}"} for index in range(1, 6)
        ],
        "consumer_wiring_contract": {
            "path": parent["wiring_contract_path"],
            "sha256": parent["wiring_contract_sha256"],
            "schema_version": mod.REPAIR05_WIRING_SCHEMA,
        },
        "rfq_resource_contract": {
            "path": parent["resource_contract_path"],
            "sha256": parent["resource_contract_sha256"],
            "schema_version": mod.REPAIR04_RESOURCE_SCHEMA,
            "current_runtime": copy.deepcopy(mod.REPAIR04_RUNTIME_CONTRACT),
        },
        "registered_rfq_query_sha256": parent[
            "registered_rfq_query_sha256"
        ],
    }
    monkeypatch.setattr(
        mod,
        "_apply_repair05_consumer_wiring",
        lambda *_args, **_kwargs: copy.deepcopy(base_result),
    )
    inputs = {
        "objects": record["coverage"]["full_unique_objects"],
        "logical_manifest_bindings": record["coverage"][
            "full_logical_manifest_bindings"
        ],
        "bytes": record["coverage"]["full_unique_bytes"],
        "path_size_fingerprint_sha256": record["coverage"][
            "full_object_set_sha256"
        ],
    }
    result = mod._apply_repair06_status_wiring(run_dir, manifest, inputs)
    assert [row["repair_id"] for row in result["repair_chain"]] == [
        "repair-01", "repair-02", "repair-03", "repair-04", "repair-05",
        "repair-06",
    ]
    assert result["validate_run_status_wiring_contract"] == {
        "path": mod.REPAIR06_STATUS_WIRING_CONTRACT,
        "sha256": record["status_wiring_contract_sha256"],
        "schema_version": mod.REPAIR06_STATUS_WIRING_SCHEMA,
    }
    assert result["expected_success_resource"] == {
        "label": mod.REPAIR06_SUCCESS_RESOURCE_LABEL,
        "path": mod.REPAIR06_SUCCESS_RESOURCE_PATH,
    }


def test_repair06_runtime_status_wiring_gate_rejects_contract_drift(tmp_path):
    contract = tmp_path / mod.REPAIR06_STATUS_WIRING_CONTRACT
    contract.parent.mkdir(parents=True)
    contract.write_bytes(b'{"schema_version":"repair06"}\n')
    binding = {
        "path": mod.REPAIR06_STATUS_WIRING_CONTRACT,
        "sha256": mod.sha256(contract),
        "schema_version": mod.REPAIR06_STATUS_WIRING_SCHEMA,
    }
    mod._enforce_registered_validate_run_status_wiring_contract(
        {"validate_run_status_wiring_contract": binding}, tmp_path
    )
    contract.write_bytes(b'{"schema_version":"drifted"}\n')
    with pytest.raises(mod.RFQStageError, match="status wiring contract changed"):
        mod._enforce_registered_validate_run_status_wiring_contract(
            {"validate_run_status_wiring_contract": binding}, tmp_path
        )


def test_validate_run_accepts_repair05_and_repair06_registered_statuses(
    tmp_path, monkeypatch
):
    run_dir, manifest = _repair06_validate_run_fixture(tmp_path, monkeypatch)
    assert mod.validate_run(run_dir)["status"] == mod.REPAIR06_STATUS
    manifest["status"] = mod.REPAIR05_STATUS
    manifest["registration_state"] = mod.REPAIR05_REGISTRATION_STATE
    write_json(run_dir / "RUN_MANIFEST.json", manifest)
    assert mod.validate_run(run_dir)["status"] == mod.REPAIR05_STATUS


def test_main_repair06_overlay_preflight_is_read_only_and_strictly_gated(
    tmp_path, monkeypatch, capsys
):
    run_dir, manifest = _repair06_validate_run_fixture(tmp_path, monkeypatch)
    active_manifest = copy.deepcopy(manifest)
    active_manifest["status"] = mod.REPAIR05_STATUS
    active_manifest["registration_state"] = mod.REPAIR05_REGISTRATION_STATE
    active_manifest["data_integrity_repairs"] = active_manifest[
        "data_integrity_repairs"
    ][:5]
    write_json(run_dir / "RUN_MANIFEST.json", active_manifest)
    overlay = run_dir / mod.REPAIR06_ROOT / "post_repair"
    write_json(overlay / "RUN_MANIFEST.json", manifest)

    state_path = run_dir / "REPORT/tables/RFQ_FULL_STAGE_STATE.json"
    input_path = run_dir / "DATA_INTEGRITY/RFQ_FULL_INPUT_IDENTITY.json"
    state_path.write_bytes(b'{"status":"UNCHANGED"}\n')
    input_path.write_bytes(b'{"schema":"UNCHANGED"}\n')
    state_before = state_path.read_bytes()
    input_before = input_path.read_bytes()
    monkeypatch.setattr(
        mod,
        "discover_inputs",
        lambda *_args: pytest.fail("input discovery ran during status preflight"),
    )
    monkeypatch.setattr(
        mod,
        "validate_input_bindings",
        lambda *_args: pytest.fail("input binding ran during status preflight"),
    )
    monkeypatch.setattr(
        mod,
        "apply_object_quarantine",
        lambda *_args, **_kwargs: pytest.fail(
            "repair replay ran during status preflight"
        ),
    )
    monkeypatch.setattr(
        duckdb,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("DuckDB opened during preflight"),
    )
    args = [
        "--run-dir", str(run_dir),
        "--cache-root", str(tmp_path / "cache"),
        "--validate-run-preflight-only",
        "--validate-run-preflight-overlay", str(overlay),
    ]
    assert mod.main(args) == 0
    assert capsys.readouterr().out.strip() == (
        f"RFQ_VALIDATE_RUN_PREFLIGHT_COMPLETE run_id={run_dir.name}"
    )
    assert state_path.read_bytes() == state_before
    assert input_path.read_bytes() == input_before
    assert not (run_dir / "cache/rfq_full_scratch.duckdb").exists()
    assert not (run_dir / "cache/rfq_full_scratch.duckdb.wal").exists()


def test_main_disk_headroom_failure_does_not_mutate_failed03_evidence(
    tmp_path, monkeypatch
):
    fixture = repair03_main_failure_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mod.shutil, "disk_usage", lambda _path: type("Usage", (), {"free": 0})()
    )
    connect_called = False

    def forbidden_connect(_path):
        nonlocal connect_called
        connect_called = True
        raise AssertionError("connect must not run after a pure preflight failure")

    monkeypatch.setattr(duckdb, "connect", forbidden_connect)
    with pytest.raises(mod.RFQStageError, match="insufficient disk headroom"):
        mod.main([
            "--run-dir", str(fixture["run_dir"]),
            "--cache-root", str(tmp_path / "cache"),
            "--min-free-gib", "1",
        ])
    assert connect_called is False
    assert fixture["input_path"].read_bytes() == fixture["input_before"]
    assert fixture["state_path"].read_bytes() == fixture["state_before"]
    assert not fixture["scratch"].exists()


def test_main_connect_failure_writes_governed_repair03_state_and_keeps_scratch(
    tmp_path, monkeypatch
):
    fixture = repair03_main_failure_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mod.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 2**50})(),
    )

    def fail_connect(path):
        Path(path).write_bytes(b"partial fresh scratch")
        raise RuntimeError("synthetic connect failure")

    monkeypatch.setattr(duckdb, "connect", fail_connect)
    with pytest.raises(RuntimeError, match="synthetic connect failure"):
        mod.main([
            "--run-dir", str(fixture["run_dir"]),
            "--cache-root", str(tmp_path / "cache"),
            "--min-free-gib", "0",
        ])
    state = json.loads(fixture["state_path"].read_text())
    assert state["status"] == "FAILED_DATA_INTEGRITY_REQUIRES_NEW_PREREGISTRATION"
    assert state["resume"] is False
    assert state["next_required_authority"] == (
        "EXPLICIT_NEW_PREREGISTRATION_OR_RFQ_SCOPE_TERMINATION"
    )
    assert state["error_type"] == "RuntimeError"
    assert fixture["scratch"].read_bytes() == b"partial fresh scratch"
    assert json.loads(fixture["input_path"].read_text())["schema"] == (
        "rfq-full-input-identity-v3"
    )


def test_main_configure_failure_closes_connection_and_keeps_governed_scratch(
    tmp_path, monkeypatch
):
    fixture = repair03_main_failure_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mod.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 2**50})(),
    )

    class SyntheticConnection:
        closed = False

        def close(self):
            self.closed = True

    connection = SyntheticConnection()

    def connect(path):
        Path(path).write_bytes(b"configured fresh scratch")
        return connection

    monkeypatch.setattr(duckdb, "connect", connect)
    monkeypatch.setattr(
        mod,
        "configure",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("synthetic configure failure")
        ),
    )
    with pytest.raises(RuntimeError, match="synthetic configure failure"):
        mod.main([
            "--run-dir", str(fixture["run_dir"]),
            "--cache-root", str(tmp_path / "cache"),
            "--min-free-gib", "0",
        ])
    state = json.loads(fixture["state_path"].read_text())
    assert state["status"] == "FAILED_DATA_INTEGRITY_REQUIRES_NEW_PREREGISTRATION"
    assert state["resume"] is False
    assert state["next_required_authority"] == (
        "EXPLICIT_NEW_PREREGISTRATION_OR_RFQ_SCOPE_TERMINATION"
    )
    assert state["error_type"] == "RuntimeError"
    assert fixture["scratch"].read_bytes() == b"configured fresh scratch"
    assert connection.closed is True


@pytest.mark.parametrize(
    "mutation",
    ("repair_order", "cumulative_drop", "coverage_fingerprint", "receipt_sha", "auth"),
)
def test_repair02_contract_mutations_fail_closed(
    tmp_path, monkeypatch, mutation
):
    fixture = double_quarantine_contract_fixture(tmp_path)
    monkeypatch.setattr(
        mod, "apply_object_quarantine",
        lambda run_dir, manifest, inputs, _ignore_repair02=False: fixture["first"],
    )
    monkeypatch.setattr(mod, "_validate_repair02_archives", lambda *args: ([], []))
    repair02 = fixture["manifest"]["data_integrity_repairs"][1]
    if mutation == "repair_order":
        fixture["manifest"]["data_integrity_repairs"].reverse()
    elif mutation == "cumulative_drop":
        declaration_path = fixture["run_dir"] / mod.QUARANTINE_DECLARATION_02
        declaration = json.loads(declaration_path.read_text())
        declaration["cumulative_quarantined_objects"] = (
            declaration["cumulative_quarantined_objects"][:1]
        )
        write_json(declaration_path, declaration)
    elif mutation == "coverage_fingerprint":
        repair02["coverage"]["retained_selection_fingerprint_sha256"] = "0" * 64
    elif mutation == "receipt_sha":
        receipt_path = fixture["run_dir"] / mod.MALFORMED_OBJECT_RECEIPT_02
        receipt = json.loads(receipt_path.read_text())
        receipt["observed_sha256"] = "0" * 64
        write_json(receipt_path, receipt)
    elif mutation == "auth":
        auth_path = fixture["run_dir"] / "DATA_INTEGRITY/REPAIR_02_USER_AUTHORIZATION.json"
        auth = json.loads(auth_path.read_text())
        auth["authorized_action"] = "forged"
        write_json(auth_path, auth)
    with pytest.raises(mod.RFQStageError):
        mod._apply_double_object_quarantine(
            fixture["run_dir"], fixture["manifest"], fixture["inputs"]
        )


def test_any_second_malformed_consumed_object_still_aborts(tmp_path):
    fixture = quarantine_fixture(tmp_path)
    inputs = mod.apply_object_quarantine(
        fixture["run_dir"], fixture["manifest"],
        mod.discover_inputs(fixture["cache"]),
    )
    with fixture["next_path"].open("a", encoding="utf-8") as handle:
        handle.write('{"second":"malformed"\n')
    connection = duckdb.connect()
    with pytest.raises(Exception, match="Malformed JSON|unexpected character"):
        mod.build_scan_tables(
            connection, inputs["paths"], fixture["run_dir"].name,
            inputs["quarantine_boundaries"],
        )
    connection.close()


def test_any_malformed_inner_payload_in_consumed_object_still_aborts(tmp_path):
    fixture = quarantine_fixture(tmp_path)
    inputs = mod.apply_object_quarantine(
        fixture["run_dir"], fixture["manifest"],
        mod.discover_inputs(fixture["cache"]),
    )
    with fixture["next_path"].open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(recorder(
            fixture["base_ns"] + 7_206_000_000_000, frame=None
        ) | {"raw": "{malformed-inner"}) + "\n")
    connection = duckdb.connect()
    with pytest.raises(mod.RFQStageError, match="malformed inner RFQ payload"):
        mod.build_scan_tables(
            connection, inputs["paths"], fixture["run_dir"].name,
            inputs["quarantine_boundaries"],
        )
    connection.close()


@pytest.mark.parametrize(
    "fields",
    [
        {"marker": "transport_close"},
        {"marker": "transport_close", "raw": None},
        {"marker": "transport_close", "raw": " "},
        {"marker": "transport_close", "raw": "{}"},
        {"marker": "transport_close", "raw": "{malformed"},
        {"marker": "Transport_close", "raw": ""},
        {"marker": "epoch_change", "raw": ""},
        {"marker": "unknown", "raw": "{}"},
        {},
        {"raw": None},
        {"raw": "42"},
        {"raw": "[]"},
        {"raw": "{malformed"},
        {"marker": "segment_receipt"},
        {"marker": "segment_receipt", "raw": "null"},
        {"marker": "segment_receipt", "raw": "[]"},
        {"marker": "segment_receipt", "raw": "{malformed"},
    ],
    ids=[
        "control-missing-raw",
        "control-null-raw",
        "control-whitespace-raw",
        "control-valid-object-payload",
        "control-malformed-payload",
        "control-case-variant",
        "epoch-change-not-preregistered",
        "unknown-marker-valid-object",
        "frame-missing-raw",
        "frame-null-raw",
        "frame-valid-scalar",
        "frame-valid-list",
        "frame-malformed-json",
        "receipt-missing-raw",
        "receipt-valid-null",
        "receipt-valid-list",
        "receipt-malformed-json",
    ],
)
def test_inner_payload_contract_rejects_every_unregistered_shape(tmp_path, fields):
    source = (
        tmp_path / "releases" / mod.RELEASE_IDS[0] / "raw_rfq" /
        "date=2026-07-12/rfq_00.ndjson"
    )
    outer = {
        "recv_wall_ns": 1_000_000_000,
        "recv_mono_ns": 1_000_000_000,
        "stream_epoch": 1,
    }
    outer.update(fields)
    write_rows(source, [outer])
    connection = duckdb.connect()
    connection.execute(mod.scan_sql([source], "invalid-inner-contract"))
    assert connection.execute("""
      SELECT expected_blank_control_marker,inner_payload_contract_valid,
        inner_payload_contract_valid IS NULL
      FROM rfq_scan_rows
    """).fetchone() == (False, False, False)
    with pytest.raises(
        mod.RFQStageError,
        match="malformed inner RFQ payload or marker contract violation",
    ):
        mod.build_scan_tables(connection, [source], "invalid-inner-contract")
    connection.close()


def test_rfq_receipt_sidecar_is_retained_and_strictly_parsed(tmp_path):
    fixture = quarantine_fixture(tmp_path)
    inputs = mod.apply_object_quarantine(
        fixture["run_dir"], fixture["manifest"],
        mod.discover_inputs(fixture["cache"]),
    )
    assert fixture["receipt_path"] in inputs["paths"]
    with fixture["receipt_path"].open("a", encoding="utf-8") as handle:
        handle.write('{"malformed-receipt-sidecar"\n')
    connection = duckdb.connect()
    with pytest.raises(Exception, match="Malformed JSON|unexpected character"):
        mod.build_scan_tables(
            connection, inputs["paths"], fixture["run_dir"].name,
            inputs["quarantine_boundaries"],
        )
    connection.close()


def test_mutation_guard_control_dim_lookback_and_synthetic_clob_stage(tmp_path):
    day = dt.datetime(2026, 7, 12, 12, tzinfo=dt.timezone.utc)
    anchor_us = int(day.timestamp() * 1_000_000)
    source = (
        tmp_path / "releases" / mod.RELEASE_IDS[0] / "raw_rfq" /
        "date=2026-07-12/rfq_12.ndjson"
    )
    write_rows(source, [
        recorder(anchor_us * 1000, frame(
            "rfq_created", "A", "M-A", "2026-07-12T12:00:00Z",
            creator_id="requester", contracts_fp="10.00", target_cost_dollars="4.000000",
            mve_collection_ticker="COLL", mve_selected_legs=[
                {"event_ticker": "E-A", "market_ticker": "M-A", "side": "yes",
                 "yes_settlement_value_dollars": "1.000000"}
            ],
        )),
        recorder((anchor_us + 2_000_000) * 1000, frame(
            "rfq_deleted", "A", "M-A", "2026-07-12T12:00:02Z", creator_id="requester"
        )),
        recorder((anchor_us + 130_000_000) * 1000,
                 {"type": "subscribed", "msg": {"channel": "communications", "sid": 7}}),
    ])
    connection = duckdb.connect()
    mod.build_scan_tables(connection, [source], "synthetic-run")
    mod.build_request_tables(connection, "synthetic-run")
    request_key = connection.execute(
        "SELECT request_key FROM rfq_requests_base"
    ).fetchone()[0]
    create_control_shift_us = connection.execute("""
      SELECT ? + abs(hash(?,?,'CREATE')) % ?
    """, [mod.CONTROL_MIN_SHIFT_US, request_key, "M-A",
           mod.CONTROL_SHIFT_SPAN_US]).fetchone()[0]
    # Control anchor is 60s after dim-effective, but its required 120s lookback
    # starts 60s before dim-effective. It must therefore remain ineligible.
    dim_effective_us = anchor_us - create_control_shift_us - 60_000_000

    core_path = tmp_path / "cycle1.duckdb"
    core = duckdb.connect(str(core_path))
    core.execute("""
      CREATE TABLE universe(date DATE,market_ticker VARCHAR,sport VARCHAR,league VARCHAR,
        root_event_id VARCHAR,root_map_status VARCHAR,occurrence_datetime TIMESTAMPTZ,
        dim_effective_us BIGINT)
    """)
    core.execute("""
      INSERT INTO universe VALUES (
        DATE '2026-07-12','M-A','Soccer','L','ROOT',
        'PROVISIONAL_HEURISTIC_MATCHUP_TIME',
        TIMESTAMPTZ '2026-07-12 13:00:00+00',?)
    """, [dim_effective_us])
    core.execute("""
      CREATE TABLE l1_real(market_ticker VARCHAR,t_us BIGINT,recv_mono_ns BIGINT,
        yes_bid_e4 INTEGER,yes_ask_e4 INTEGER,yes_bid_qty_e4 BIGINT,yes_ask_qty_e4 BIGINT)
    """)
    l1 = []
    for offset_s in range(-700, 132):
        t_us = anchor_us + offset_s * 1_000_000
        l1.append(("M-A", t_us, t_us * 1000, 3900, 4100, 100_000, 100_000))
    core.executemany("INSERT INTO l1_real VALUES (?,?,?,?,?,?,?)", l1)
    core.execute("CREATE TABLE trades_safe(market_ticker VARCHAR,t_us BIGINT,taker_sign INTEGER,count_e4 BIGINT)")
    core.execute("INSERT INTO trades_safe VALUES ('M-A',?,1,10000)", [anchor_us + 1_000_000])
    core.execute("CREATE TABLE l2_all(market_ticker VARCHAR,t_us BIGINT)")
    core.executemany("INSERT INTO l2_all VALUES ('M-A',?)", [
        [anchor_us - 2_000_000], [anchor_us + 1_000_000]
    ])
    core.execute("CREATE TABLE capture_gaps(start_us BIGINT,end_us BIGINT)")
    core.close()

    mod.attach_core_and_enrich(connection, core_path)
    mod.build_descriptive_tables(connection)
    result = mod.build_clob_context(connection, max_per_root=50)
    assert result["candidate_anchor_markets"] == 2
    assert result["candidate_endpoint_anchors"] == 4
    assert result["causal_eligible_endpoint_anchors"] == 4
    assert result["posthoc_endpoint_anchors_excluded"] == 0
    assert result["sampled_endpoint_anchors"] == 4
    assert result["control_anchors_excluded_before_dim_effective"] >= 2
    assert (result["control_anchors_excluded_before_dim_effective"]
            + result["control_anchors_dim_eligible"] == 4)
    assert result["clob_context_rows"] == 72
    assert connection.execute("""
      SELECT count(*) FROM rfq_clob_anchors
      WHERE endpoint_type='CREATE' AND control_anchor_us>=dim_effective_us
        AND control_earliest_required_us<dim_effective_us
        AND NOT control_dim_eligible
    """).fetchone()[0] == 2
    assert connection.execute("""
      SELECT count(*) FROM rfq_clob_context
      WHERE endpoint_type='CREATE' AND cohort='PRIOR_CONTROL' AND valid_receive_window
    """).fetchone()[0] == 0
    assert connection.execute("""
      SELECT count(*) FROM rfq_control_balance
      WHERE endpoint_type='CREATE' AND matched
    """).fetchone()[0] == 0
    assert connection.execute(
        "SELECT count(*) FROM rfq_clob_context WHERE valid_receive_window"
    ).fetchone()[0] > 0
    # Strict ASOF: the trade at +1s is excluded from the boundary exactly at +1s.
    exact = connection.execute("""
      SELECT trades FROM rfq_clob_context
      WHERE endpoint_type='CREATE' AND cohort='RFQ' AND window_label='p100ms_1s'
    """).fetchone()[0]
    assert exact == 0
    assert connection.execute("SELECT count(*) FROM rfq_combo_clob_proxy").fetchone()[0] == 1
    run_dir = tmp_path / "synthetic-run"
    exports = mod.export_outputs(connection, run_dir)
    charts = mod.render_charts(connection, run_dir) if importlib.util.find_spec("matplotlib") else []
    summary = mod.build_summary(connection, {
        "run_id": "synthetic-run",
        "cycle1_duckdb_binding": synthetic_cycle1_binding(),
        "objects": 1, "bytes": source.stat().st_size,
        "logical_manifest_bindings": 1, "deduplicated_overlapping_objects": 0,
        "overlap_keys": [],
        "path_size_fingerprint_sha256": "a" * 64,
        "coverage_status": "COMPLETE_MANIFEST_OBJECT_COVERAGE",
        "full_object_coverage": True,
        "unique_objects_total": 1,
        "unique_bytes_total": source.stat().st_size,
        "consumed_unique_objects": 1,
        "consumed_logical_bindings": 1,
        "consumed_bytes": source.stat().st_size,
        "consumed_object_set_sha256": "a" * 64,
        "quarantined_unique_objects": 0,
        "quarantined_logical_bindings": 0,
        "quarantined_bytes": 0,
        "quarantined_object_set_sha256": hashlib.sha256(b"").hexdigest(),
        "quarantine_reasons": [],
        "quarantine_details": [],
        "selection_fingerprint_sha256": "a" * 64,
    }, result, 1.0)
    assert len(exports["parquet_tables"]) == 4
    assert "REPORT/tables/rfq_observation_boundaries.csv" in exports["csv_tables"]
    assert len(charts) in (0, 9)
    assert summary["hard_truth"]["broadcast_contains_accepted_quote_or_fill"] is False
    connection.close()
    mod.write_catalog(run_dir)
    catalog = duckdb.connect(str(run_dir / "cache/rfq_full_catalog.duckdb"), read_only=True)
    assert catalog.execute("SELECT count(*) FROM rfq_requests").fetchone()[0] == 1
    catalog.close()
