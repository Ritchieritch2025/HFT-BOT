import importlib.util
import copy
import csv
import hashlib
import json
from pathlib import Path

import pytest


path = Path(__file__).with_name("finalize_mission.py")
spec = importlib.util.spec_from_file_location("finalize_mission", path)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mod)

repair04_registration_path = Path(__file__).with_name("repair04_registration.py")
repair04_spec = importlib.util.spec_from_file_location(
    "repair04_registration_for_finalizer_tests", repair04_registration_path
)
repair04_registrar = importlib.util.module_from_spec(repair04_spec)
assert repair04_spec.loader is not None
repair04_spec.loader.exec_module(repair04_registrar)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def make_run(tmp_path):
    run = tmp_path / "20260715T112538Z__c21a79a8cff__deep01"
    (run / "REPORT/tables").mkdir(parents=True)
    (run / "REPORT/charts").mkdir(parents=True)
    (run / "DATA_INTEGRITY").mkdir(parents=True)
    (run / "queries").mkdir(parents=True)
    (run / "REPORT/charts/chart.png").write_bytes(b"fixture-png")
    (run / "REPORT/tables/data_coverage_cube.csv").write_text(
        "date,channel,rows\n2026-07-12,orderbooks_l1,1\n", encoding="utf-8"
    )
    attestation = {
        "instance_id": "i-0e53d134dceffe166",
        "region": "us-east-2",
        "role": "w09-research-runner",
        "instance_profile": "w09-research-runner",
        "architecture": "aarch64",
        "duckdb": "1.4.5",
        "w09_run_inhibitor_present": True,
        "w09_run_inhibitor_is_ancestor": True,
        "static_credentials_present": False,
        "trading_credentials_present": False,
        "ambient_aws_or_kalshi_variables": [],
        "static_credential_paths_present": [],
        "installation_sha256": mod.EXPECTED_W09_INSTALLATION_SHA256,
        "s3_access": "READ_ONLY_RESEARCH_PREFIX",
    }
    write_json(run / "DATA_INTEGRITY/W09_ATTESTATION.json", attestation)
    frozen_finalizer = run / "queries/finalize_mission.py"
    frozen_finalizer.write_bytes(path.read_bytes())
    frozen_rfq = run / "queries/rfq_full_stage.py"
    frozen_rfq.write_text("# frozen RFQ repair fixture\n", encoding="utf-8")
    (run / "source").mkdir()
    (run / "source/rfq_full_stage.py").write_bytes(frozen_rfq.read_bytes())
    query_sums = run / "QUERY_SHA256SUMS.txt"
    query_sums.write_text(
        f"{mod.sha256(frozen_finalizer)}  queries/finalize_mission.py\n"
        f"{mod.sha256(frozen_rfq)}  queries/rfq_full_stage.py\n",
        encoding="utf-8",
    )
    write_json(run / "SOURCE_MANIFEST.json", [])
    (run / "SOURCE_SHA256SUMS.txt").write_text(
        f"{mod.sha256(run / 'source/rfq_full_stage.py')}  source/rfq_full_stage.py\n",
        encoding="utf-8",
    )
    write_json(run / "FEATURE_DICTIONARY.json", {"features": []})
    (run / "METHODS.md").write_text("# Frozen methods\n", encoding="utf-8")
    previous_commit = "9" * 40
    current_commit = "8" * 40
    quarantine_key = "raw_rfq/date=2026-07-14/rfq_00.ndjson"
    quarantine_sha = "c" * 64
    quarantine_size = 50
    quarantine_version = "fixture-version-id"
    quarantine_release = list(mod.EXPECTED_RELEASE_BINDINGS)[1]
    quarantine_reason = (
        "Exact sealed bytes contain a structurally malformed NDJSON record; "
        "no line-level salvage is permitted."
    )
    version_dir = run / "DATA_INTEGRITY/version_ids"
    version_dir.mkdir(parents=True)
    (version_dir / "2026-07-12.jsonl").write_text(
        json.dumps(
            {
                "key": "raw_rfq/date=2026-07-13/rfq_00.ndjson",
                "sha256": "a" * 64,
                "size": 100,
                "version_id": "fixture-a",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (version_dir / "2026-07-13.jsonl").write_text(
        json.dumps(
            {
                "key": quarantine_key,
                "sha256": quarantine_sha,
                "size": quarantine_size,
                "version_id": quarantine_version,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    release_ids = list(mod.EXPECTED_RELEASE_BINDINGS)
    manifest_dir = run / "DATA_INTEGRITY/manifests"
    manifest_dir.mkdir(parents=True)
    fixture_manifests = {
        release_ids[0]: {
            "release_id": release_ids[0],
            "date": mod.EXPECTED_RELEASE_BINDINGS[release_ids[0]]["date"],
            "objects": [
                {
                    "key": "raw_rfq/date=2026-07-13/rfq_00.ndjson",
                    "sha256": "a" * 64,
                    "size": 100,
                    "version_id": "fixture-a",
                }
            ],
        },
        release_ids[1]: {
            "release_id": release_ids[1],
            "date": mod.EXPECTED_RELEASE_BINDINGS[release_ids[1]]["date"],
            "objects": [
                {
                    "key": quarantine_key,
                    "sha256": quarantine_sha,
                    "size": quarantine_size,
                    "version_id": quarantine_version,
                },
                {
                    "key": "raw_rfq/date=2026-07-14/rfq_01.ndjson",
                    "sha256": "b" * 64,
                    "size": 100,
                    "version_id": "fixture-b",
                },
            ],
        },
    }
    for release_id, value in fixture_manifests.items():
        manifest_path = manifest_dir / f"{release_id}.json"
        write_json(manifest_path, value)
        mod.EXPECTED_RELEASE_BINDINGS[release_id]["manifest_sha256"] = mod.sha256(
            manifest_path
        )
    declaration = {
        "schema_version": "rfq-object-quarantine-v1",
        "run_id": run.name,
        "mode": mod.EXPECTED_MODE,
        "finding": "MALFORMED_NDJSON_OBJECT",
        "disposition": "WHOLE_OBJECT_QUARANTINE",
        "created_after_structural_failure_before_rfq_result": True,
        "dependent_rfq_result_opened": False,
        "remaining_object_parse_policy": (
            "STRICT_NDJSON_IGNORE_ERRORS_FALSE; any additional malformed object aborts"
        ),
        "authority_basis": "Mission section 22 isolated-channel quarantine authority.",
        "selection_rule": "One exact manifest object with a strict malformed receipt.",
        "result_use_prohibited": "No quarantined row or result may be used.",
        "source_execution_commit": previous_commit,
        "quarantined_objects": [
            {
                "release_id": quarantine_release,
                "key": quarantine_key,
                "version_id": quarantine_version,
                "size": quarantine_size,
                "sha256": quarantine_sha,
                "manifest_sha256": mod.EXPECTED_RELEASE_BINDINGS[
                    quarantine_release
                ]["manifest_sha256"],
                "receipt": mod.RFQ_MALFORMED_RECEIPT.as_posix(),
                "invalid_line_count": 1,
                "reason": quarantine_reason,
            }
        ],
    }
    receipt = {
        "schema_version": "rfq-malformed-object-receipt-v1",
        "run_id": run.name,
        "disposition": "CHANNEL_OBJECT_QUARANTINE_REQUIRED",
        "release_id": quarantine_release,
        "key": quarantine_key,
        "expected_size": quarantine_size,
        "observed_size": quarantine_size,
        "expected_sha256": quarantine_sha,
        "observed_sha256": quarantine_sha,
        "raw_payload_redacted": True,
        "total_lines": 10,
        "invalid_line_count": 1,
        "invalid_lines": [
            {
                "line_number": 8,
                "line_bytes": 20,
                "line_sha256": "d" * 64,
                "error_type": "JSONDecodeError",
            }
        ],
    }
    write_json(run / mod.RFQ_DECLARATION, declaration)
    write_json(run / mod.RFQ_MALFORMED_RECEIPT, receipt)
    write_json(run / mod.REPAIR_DECLARATION, declaration)
    write_json(run / mod.REPAIR_MALFORMED_RECEIPT, receipt)

    pre_root = run / "DATA_INTEGRITY/repairs/repair-01/pre_repair"
    write_json(pre_root / "SOURCE_MANIFEST.json", {"source": "pre-repair"})
    (pre_root / "SOURCE_SHA256SUMS.txt").write_text(
        f"{'d' * 64}  source/rfq_full_stage.py\n", encoding="utf-8"
    )
    (pre_root / "QUERY_SHA256SUMS.txt").write_text(
        f"{'e' * 64}  queries/finalize_mission.py\n", encoding="utf-8"
    )
    previous_source_sha = mod.sha256(pre_root / "SOURCE_MANIFEST.json")
    previous_source_sums_sha = mod.sha256(pre_root / "SOURCE_SHA256SUMS.txt")
    previous_query_sha = mod.sha256(pre_root / "QUERY_SHA256SUMS.txt")
    failed_fingerprint = mod.object_set_sha256(
        [
            {
                "key": "raw_rfq/date=2026-07-13/rfq_00.ndjson",
                "sha256": "a" * 64,
                "size": 100,
            },
            {
                "key": quarantine_key,
                "sha256": quarantine_sha,
                "size": quarantine_size,
            },
            {
                "key": "raw_rfq/date=2026-07-14/rfq_01.ndjson",
                "sha256": "b" * 64,
                "size": 100,
            },
        ]
    )
    failed_state = {
        "schema": "rfq-full-stage-state-v1",
        "status": "FAILED_RESUMABLE",
        "resume": False,
        "scratch": "/srv/w09-research/runs/fixture/cache/rfq_full_scratch.duckdb",
        "input_fingerprint": failed_fingerprint,
        "error": f"Malformed JSON in {quarantine_key}",
        "error_type": "InvalidInputException",
    }
    failed_resource = {
        "schema_version": "w09-stage-resource-v1",
        "label": "rfq_full_stage",
        "return_code": 1,
        "command": ["python3", "rfq_full_stage.py", "--run-dir", str(run)],
    }
    failed_scratch = {
        "schema_version": "rfq-failed-scratch-receipt-v1",
        "run_id": run.name,
        "original_scratch_path": failed_state["scratch"],
        "preserved_scratch_path": failed_state["scratch"].replace(
            ".duckdb", ".attempt01_failed.duckdb"
        ),
        "sha256": "6" * 64,
        "bytes": 123,
        "mtime_utc": "2026-07-15T13:20:32+00:00",
        "input_fingerprint": failed_fingerprint,
        "disposition": "PRESERVED_RENAMED_NO_RESUME",
        "resume_allowed": False,
    }
    write_json(run / mod.FAILED_RFQ_STATE, failed_state)
    write_json(run / mod.FAILED_RFQ_RESOURCE, failed_resource)
    write_json(run / mod.FAILED_RFQ_SCRATCH_RECEIPT, failed_scratch)
    failed_state_sha = mod.sha256(run / mod.FAILED_RFQ_STATE)
    failed_resource_sha = mod.sha256(run / mod.FAILED_RFQ_RESOURCE)
    failed_scratch_sha = mod.sha256(run / mod.FAILED_RFQ_SCRATCH_RECEIPT)
    current_source_sha = mod.sha256(run / "SOURCE_MANIFEST.json")
    current_source_sums_sha = mod.sha256(run / "SOURCE_SHA256SUMS.txt")
    current_query_sha = mod.sha256(query_sums)
    query_files = ["queries/finalize_mission.py", "queries/rfq_full_stage.py"]
    previous_identity = {
        "execution_commit": previous_commit,
        "source_manifest_sha256": previous_source_sha,
        "source_sha256s_sha256": previous_source_sums_sha,
        "query_set_sha256": previous_query_sha,
        "query_files": query_files,
    }
    current_identity = {
        "execution_commit": current_commit,
        "source_manifest_sha256": current_source_sha,
        "source_sha256s_sha256": current_source_sums_sha,
        "query_set_sha256": current_query_sha,
        "query_files": query_files,
    }
    repair = {
        "schema_version": "sports-autoresearch-data-integrity-repair-v1",
        "repair_id": "repair-01",
        "finding": "MALFORMED_NDJSON_OBJECT",
        "pre_repair_status": "CYCLE1_CORE_COMPLETE_RFQ_FULL_SCAN_PENDING",
        "post_repair_status": mod.REPAIR_PENDING_STATUS,
        "rfq_result_state": "NO_RFQ_RESULT_OPENED",
        "quarantine_policy": "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE",
        "quarantined_objects": declaration["quarantined_objects"],
        "declaration_input_path": mod.RFQ_DECLARATION.as_posix(),
        "receipt_input_path": mod.RFQ_MALFORMED_RECEIPT.as_posix(),
        "declaration_path": mod.REPAIR_DECLARATION.as_posix(),
        "declaration_sha256": mod.sha256(run / mod.RFQ_DECLARATION),
        "receipt_path": mod.REPAIR_MALFORMED_RECEIPT.as_posix(),
        "receipt_sha256": mod.sha256(run / mod.RFQ_MALFORMED_RECEIPT),
        "previous_execution_commit": previous_commit,
        "current_execution_commit": current_commit,
        "previous_source_manifest_sha256": previous_source_sha,
        "current_source_manifest_sha256": current_source_sha,
        "previous_source_sha256s_sha256": previous_source_sums_sha,
        "current_source_sha256s_sha256": current_source_sums_sha,
        "previous_query_set_sha256": previous_query_sha,
        "current_query_set_sha256": current_query_sha,
        "previous_repository_identity": previous_identity,
        "current_repository_identity": current_identity,
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
        "failed_state_path": mod.FAILED_RFQ_STATE.as_posix(),
        "failed_state_sha256": failed_state_sha,
        "failed_resource_receipt_path": mod.FAILED_RFQ_RESOURCE.as_posix(),
        "failed_resource_receipt_sha256": failed_resource_sha,
        "failed_scratch_receipt_path": mod.FAILED_RFQ_SCRATCH_RECEIPT.as_posix(),
        "failed_scratch_receipt_sha256": failed_scratch_sha,
        "trial_registry": {
            "strict_previous_bytes_prefix": True,
            "appended_records": 2,
        },
    }
    manifest = {
        "run_id": run.name,
        "mode": mod.EXPECTED_MODE,
        "explicit_degraded_admission": True,
        "repo_commit": "c21a79a8cfffc3e2140cc07d01eda06d53bd82c3",
        "mission": {"sha256": mod.EXPECTED_MISSION_SHA},
        "canonical_prompt": {"sha256": "5" * 64},
        "banners": {
            "data_evidence": mod.EXPECTED_EVIDENCE,
            "experiment_split": mod.EXPECTED_SPLIT,
            "timestamp_discipline": mod.EXPECTED_TIMESTAMP,
            "authorization": mod.NO_LIVE_BANNER,
        },
        "gates": {
            "gate_a": {"status": "PASS_W05_EXPLORATORY_READY"},
            "gate_b": {
                "status": "PASS_W09_ATTESTED",
                "attestation_sha256": mod.sha256(
                    run / "DATA_INTEGRITY/W09_ATTESTATION.json"
                ),
            },
            "gate_c": {"status": "PASS_MODE1_ONLY", "mode2_authorized": False},
        },
        "selected_releases": [
            {
                "date": binding["date"],
                "release_id": release_id,
                "manifest_sha256": binding["manifest_sha256"],
                "object_version_set_sha256": binding["object_version_set_sha256"],
                "version_bindings_sha256": binding["version_bindings_sha256"],
                "seal_sha256": char * 64,
                "publication_state_sha256": char * 64,
                "evidence_tier": mod.EXPECTED_EVIDENCE,
                "tl1_status": mod.EXPECTED_TIMESTAMP,
                "include": mod.EXPECTED_SPLIT,
                "exact_version_list_path": (
                    f"DATA_INTEGRITY/version_ids/{binding['date']}.jsonl"
                ),
                "byte_count": 100 + index,
                "object_count": 10 + index,
            }
            for index, ((release_id, binding), char) in enumerate(
                zip(mod.EXPECTED_RELEASE_BINDINGS.items(), ("a", "b"))
            )
        ],
        "compute": {"instance_id": "i-w09", "instance_type": "r8g.2xlarge"},
        "repository": {
            "source_manifest_path": "SOURCE_MANIFEST.json",
            "execution_commit": current_commit,
            "initial_execution_commit": previous_commit,
            "previous_execution_commit": previous_commit,
            "source_manifest_sha256": current_source_sha,
            "initial_source_manifest_sha256": previous_source_sha,
            "previous_source_manifest_sha256": previous_source_sha,
            "source_sha256s_sha256": current_source_sums_sha,
            "initial_source_sha256s_sha256": previous_source_sums_sha,
            "previous_source_sha256s_sha256": previous_source_sums_sha,
            "feature_definition_sha256": mod.sha256(run / "FEATURE_DICTIONARY.json"),
            "method_definition_sha256": mod.sha256(run / "METHODS.md"),
            "query_set_sha256": current_query_sha,
            "initial_query_set_sha256": previous_query_sha,
            "previous_query_set_sha256": previous_query_sha,
            "registration_repair_id": "repair-01",
            "query_files": query_files,
            "initial_identity": previous_identity,
            "previous_identity": previous_identity,
        },
        "publication": {
            "canonical_local_archive": str(run),
            "s3_report_archive_status": "DEFERRED_AUTHORITY_CONFLICT",
        },
        "status": mod.REPAIR_PENDING_STATUS,
        "registration_state": "RE_FROZEN_AFTER_DATA_INTEGRITY_REPAIR_BEFORE_RFQ_RESULT",
        "data_integrity_repairs": [repair],
    }
    write_json(run / "RUN_MANIFEST.json", manifest)
    cards = []
    for hypothesis_id in mod.HYPOTHESIS_IDS:
        card = {
            "hypothesis_id": hypothesis_id,
            "status": "CANDIDATE",
            "split": mod.EXPECTED_SPLIT,
            "data_evidence": mod.EXPECTED_EVIDENCE,
            "sentence": f"Frozen sentence for {hypothesis_id}.",
            "mechanism": "Frozen mechanism.",
            "population": "Frozen population.",
            "feature": "Frozen causal feature.",
            "decision_clock": "TL1 receive clock.",
            "test_family": "Root-event block test.",
            "multiplicity_policy": "BH-FDR within family.",
            "negative_controls": ["future shift"],
            "required_engine_capabilities": ["causal as-of"],
            "fill_policy": "strict-through",
            "fee_policy": "exact known fees only",
            "rejection": "frozen kill rule",
            "reopen": "more sealed days",
            "economic_threshold": "frozen threshold",
        }
        if hypothesis_id in mod.RV_IDS:
            card["status"] = "DATA_STARVED"
            card["cycle1_result"] = {
                "hypothesis_status": "DATA_STARVED",
                "reason": "Authoritative family mapping is absent.",
            }
        cards.append(card)
    write_json(run / "HYPOTHESIS_LEDGER.json", {"hypotheses": cards})
    trial_before = "".join(
        json.dumps({"trial_id": item, "status": "REGISTERED"}) + "\n"
        for item in mod.HYPOTHESIS_IDS
    ).encode("utf-8")
    (pre_root / "TRIAL_REGISTRY.jsonl").write_bytes(trial_before)
    for relative in query_files:
        target = pre_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((run / relative).read_bytes())
    write_json(
        pre_root / "RUN_MANIFEST.json",
        {
            "run_id": run.name,
            "status": "CYCLE1_CORE_COMPLETE_RFQ_FULL_SCAN_PENDING",
            "repository": {
                key: value
                for key, value in previous_identity.items()
                if key != "source_sha256s_sha256"
            },
        },
    )
    trial_common = {
        "recorded_at_utc": "2026-07-15T13:30:00Z",
        "trial_ids": list(mod.RFQ_TRIAL_ORDER),
        "result_opened": False,
        "hypothesis_conclusion_opened": False,
    }
    failure_trial = {
        **trial_common,
        "trial_registration_id": "RFQ_FULL_STAGE_ATTEMPT_01",
        "record_type": "STAGE_FAILURE",
        "stage": "RFQ_FULL_STAGE",
        "failure_class": "MALFORMED_NDJSON_OBJECT",
        "failure_disposition": "STRUCTURAL_INPUT_FAILURE_BEFORE_RESULT",
        "hypothesis_conclusion": "NONE",
        "receipt_path": mod.REPAIR_MALFORMED_RECEIPT.as_posix(),
        "receipt_sha256": mod.sha256(run / mod.RFQ_MALFORMED_RECEIPT),
        "failure_state_path": mod.FAILED_RFQ_STATE.as_posix(),
        "failure_state_sha256": failed_state_sha,
        "failure_resource_path": mod.FAILED_RFQ_RESOURCE.as_posix(),
        "failure_resource_sha256": failed_resource_sha,
        "failed_scratch_receipt_path": mod.FAILED_RFQ_SCRATCH_RECEIPT.as_posix(),
        "failed_scratch_receipt_sha256": failed_scratch_sha,
        "execution_commit": previous_commit,
        "source_manifest_sha256": previous_source_sha,
        "query_set_sha256": previous_query_sha,
    }
    repair_trial = {
        **trial_common,
        "trial_registration_id": "RFQ_OBJECT_QUARANTINE_REPAIR_01",
        "record_type": "DATA_INTEGRITY_REPAIR_PREREGISTRATION",
        "stage": "RFQ_FULL_STAGE_REPAIR_PREREGISTRATION",
        "finding": "MALFORMED_NDJSON_OBJECT",
        "quarantine_policy": "DETERMINISTIC_WHOLE_OBJECT_QUARANTINE",
        "rfq_result_state": "NO_RFQ_RESULT_OPENED",
        "declaration_path": mod.REPAIR_DECLARATION.as_posix(),
        "declaration_sha256": mod.sha256(run / mod.RFQ_DECLARATION),
        "receipt_path": mod.REPAIR_MALFORMED_RECEIPT.as_posix(),
        "receipt_sha256": mod.sha256(run / mod.RFQ_MALFORMED_RECEIPT),
        "previous_execution_commit": previous_commit,
        "current_execution_commit": current_commit,
        "previous_source_manifest_sha256": previous_source_sha,
        "current_source_manifest_sha256": current_source_sha,
        "previous_query_set_sha256": previous_query_sha,
        "current_query_set_sha256": current_query_sha,
        "threshold_feature_test_or_hypothesis_status_changed": False,
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
        "core_results_recomputed": False,
    }
    appended_trial_bytes = "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
        for row in (failure_trial, repair_trial)
    ).encode("utf-8")
    trial_after = trial_before + appended_trial_bytes
    (run / "TRIAL_REGISTRY.jsonl").write_bytes(trial_after)
    trial_binding = {
        "previous_sha256": hashlib.sha256(trial_before).hexdigest(),
        "current_sha256": hashlib.sha256(trial_after).hexdigest(),
        "previous_bytes": len(trial_before),
        "current_bytes": len(trial_after),
        "strict_previous_bytes_prefix": True,
        "appended_records": 2,
        "trial_registration_ids": [
            "RFQ_FULL_STAGE_ATTEMPT_01",
            "RFQ_OBJECT_QUARANTINE_REPAIR_01",
        ],
    }
    manifest_path = run / "RUN_MANIFEST.json"
    manifest_with_trials = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_with_trials["data_integrity_repairs"][0]["trial_registry"] = trial_binding
    repair_receipt_record = manifest_with_trials["data_integrity_repairs"][0]
    write_json(run / mod.REPAIR_REGISTRATION_RECEIPT, repair_receipt_record)
    repair_receipt_sha = mod.sha256(run / mod.REPAIR_REGISTRATION_RECEIPT)
    manifest_with_trials["data_integrity_repairs"][0].update(
        {
            "repair_receipt_path": mod.REPAIR_REGISTRATION_RECEIPT.as_posix(),
            "repair_receipt_sha256": repair_receipt_sha,
        }
    )
    write_json(manifest_path, manifest_with_trials)
    write_json(
        run / mod.CORE_SUMMARY,
        {
            "run_id": run.name,
            "boundary": "No result is a promotion; RFQ full stage remains pending.",
            "atlas": {"status": "DIAGNOSTIC_ONLY"},
        },
    )
    write_json(
        run / mod.CORE_HYPOTHESIS_SUMMARY,
        {
            "schema_version": "sports-autoresearch-core-hypothesis-tests-v1",
            "run_id": run.name,
            "mode": mod.EXPECTED_MODE,
            "data_evidence": mod.EXPECTED_EVIDENCE,
            "hypotheses": {
                hypothesis_id: {
                    "hypothesis_id": hypothesis_id,
                    "hypothesis_status": (
                        "DATA_STARVED"
                        if hypothesis_id == "C1-SPREAD-CAPTURE-01"
                        else "COLLECT_MORE"
                    ),
                    "status_reason": "Frozen economics unavailable."
                    if hypothesis_id == "C1-SPREAD-CAPTURE-01"
                    else "Only one evaluation day block.",
                }
                for hypothesis_id in mod.CORE_TEST_IDS
            },
        },
    )
    consumed_rows = [
        {
            "key": "raw_rfq/date=2026-07-13/rfq_00.ndjson",
            "sha256": "a" * 64,
            "size": 100,
            "bound_release_ids": [list(mod.EXPECTED_RELEASE_BINDINGS)[0]],
        },
        {
            "key": "raw_rfq/date=2026-07-14/rfq_01.ndjson",
            "sha256": "b" * 64,
            "size": 100,
            "bound_release_ids": [quarantine_release],
        },
    ]
    consumed_set_sha = mod.object_set_sha256(consumed_rows)
    quarantine_set_sha = mod.object_set_sha256(declaration["quarantined_objects"])
    manifest_set_sha = mod.object_set_sha256(
        [*consumed_rows, declaration["quarantined_objects"][0]]
    )
    selection_sha = mod.canonical_json_sha256(
        {
            "total": manifest_set_sha,
            "consumed": consumed_set_sha,
            "quarantined": quarantine_set_sha,
            "receipt": mod.sha256(run / mod.RFQ_MALFORMED_RECEIPT),
            "declaration": mod.sha256(run / mod.RFQ_DECLARATION),
        }
    )
    authoritative_union = sorted(
        [
            consumed_rows[0],
            {
                "key": quarantine_key,
                "sha256": quarantine_sha,
                "size": quarantine_size,
                "bound_release_ids": [quarantine_release],
            },
            consumed_rows[1],
        ],
        key=lambda row: row["key"],
    )
    failed_input_identity = {
        "schema": "rfq-full-input-identity-v1",
        "release_ids": release_ids,
        "releases": [
            {
                "release_id": release_ids[0],
                "manifest_sha256": mod.EXPECTED_RELEASE_BINDINGS[release_ids[0]][
                    "manifest_sha256"
                ],
                "rfq_objects": 1,
                "rfq_bytes": 100,
                "verified_marker_sha256": "1" * 64,
            },
            {
                "release_id": release_ids[1],
                "manifest_sha256": mod.EXPECTED_RELEASE_BINDINGS[release_ids[1]][
                    "manifest_sha256"
                ],
                "rfq_objects": 2,
                "rfq_bytes": 150,
                "verified_marker_sha256": "2" * 64,
            },
        ],
        "objects": authoritative_union,
        "logical_manifest_bindings": 3,
        "unique_objects": 3,
        "deduplicated_overlapping_objects": 0,
        "path_size_sha_fingerprint": manifest_set_sha,
    }
    write_json(run / mod.FAILED_RFQ_INPUT_IDENTITY, failed_input_identity)
    failed_input_sha = mod.sha256(run / mod.FAILED_RFQ_INPUT_IDENTITY)

    (run / "cache").mkdir(exist_ok=True)
    cycle_db = run / "cache/cycle1.duckdb"
    cycle_db.write_bytes(b"fixture-cycle1-duckdb")
    write_json(
        run / "logs/resources/cycle1_core.json",
        {
            "schema_version": "w09-stage-resource-v1",
            "label": "cycle1_core",
            "return_code": 0,
        },
    )
    cycle_receipt = {
        "schema_version": "cycle1-derived-duckdb-binding-v1",
        "run_id": run.name,
        "path": f"/srv/w09-research/runs/{run.name}/cache/cycle1.duckdb",
        "bytes": cycle_db.stat().st_size,
        "sha256": mod.sha256(cycle_db),
        "mtime_utc": "2026-07-15T12:58:24+00:00",
        "duckdb_version": "1.4.5",
        "created_before_rfq_repair_registration": True,
        "core_result_disposition": "CORE_DERIVED_DATABASE_PRESERVED_NOT_RECOMPUTED",
        "core_summary_path": mod.CORE_SUMMARY.as_posix(),
        "core_summary_sha256": mod.sha256(run / mod.CORE_SUMMARY),
        "core_stage_resource_path": "logs/resources/cycle1_core.json",
        "core_stage_resource_sha256": mod.sha256(
            run / "logs/resources/cycle1_core.json"
        ),
    }
    write_json(run / mod.ACTIVE_CYCLE1_DUCKDB_BINDING, cycle_receipt)
    write_json(run / mod.ARCHIVED_CYCLE1_DUCKDB_BINDING, cycle_receipt)
    cycle_receipt_sha = mod.sha256(run / mod.ACTIVE_CYCLE1_DUCKDB_BINDING)
    cycle_binding = {
        "active_path": mod.ACTIVE_CYCLE1_DUCKDB_BINDING.as_posix(),
        "active_sha256": cycle_receipt_sha,
        "archived_path": mod.ARCHIVED_CYCLE1_DUCKDB_BINDING.as_posix(),
        "archived_sha256": cycle_receipt_sha,
        "schema_version": "cycle1-derived-duckdb-binding-v1",
        "run_id": run.name,
        "duckdb_path": cycle_receipt["path"],
        "duckdb_bytes": cycle_receipt["bytes"],
        "duckdb_sha256": cycle_receipt["sha256"],
        "core_stage_resource_path": cycle_receipt["core_stage_resource_path"],
        "core_stage_resource_sha256": cycle_receipt[
            "core_stage_resource_sha256"
        ],
        "core_summary_path": cycle_receipt["core_summary_path"],
        "core_summary_sha256": cycle_receipt["core_summary_sha256"],
    }
    manifest_path = run / "RUN_MANIFEST.json"
    manifest_with_bindings = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_with_bindings["data_integrity_repairs"][0].update(
        {
            "failed_input_identity_path": mod.FAILED_RFQ_INPUT_IDENTITY.as_posix(),
            "failed_input_identity_sha256": failed_input_sha,
            "cycle1_duckdb_binding": cycle_binding,
        }
    )
    write_json(manifest_path, manifest_with_bindings)
    quarantine_detail = {
        **declaration["quarantined_objects"][0],
        "receipt_sha256": mod.sha256(run / mod.RFQ_MALFORMED_RECEIPT),
        "declaration_sha256": mod.sha256(run / mod.RFQ_DECLARATION),
    }
    quarantine_detail.pop("receipt")
    failed_binding = {
        "repair_id": "repair-01",
        "failed_state_path": mod.FAILED_RFQ_STATE.as_posix(),
        "failed_state_sha256": failed_state_sha,
        "failed_resource_receipt_path": mod.FAILED_RFQ_RESOURCE.as_posix(),
        "failed_resource_receipt_sha256": failed_resource_sha,
        "failed_scratch_receipt_path": mod.FAILED_RFQ_SCRATCH_RECEIPT.as_posix(),
        "failed_scratch_receipt_sha256": failed_scratch_sha,
        "failed_input_identity_path": mod.FAILED_RFQ_INPUT_IDENTITY.as_posix(),
        "failed_input_identity_sha256": failed_input_sha,
    }
    gap = {
        "release_id": quarantine_release,
        "key": quarantine_key,
        "sha256": quarantine_sha,
        "gap_start_ns": 1001,
        "gap_end_ns": 3001,
        "gap_start_us": 1,
        "gap_end_us": 4,
        "boundary_reason": "WHOLE_OBJECT_QUARANTINE_MALFORMED_NDJSON",
    }
    partial_input = {
        "release_ids": list(mod.EXPECTED_RELEASE_BINDINGS),
        "coverage_status": mod.RFQ_PARTIAL_STATUS,
        "full_object_coverage": False,
        "whole_object_quarantine": True,
        "line_salvage": False,
        "logical_manifest_bindings_total": 3,
        "unique_objects_total": 3,
        "unique_bytes_total": 250,
        "consumed_unique_objects": 2,
        "consumed_logical_bindings": 2,
        "consumed_bytes": 200,
        "consumed_object_set_sha256": consumed_set_sha,
        "quarantined_unique_objects": 1,
        "quarantined_logical_bindings": 1,
        "quarantined_bytes": quarantine_size,
        "quarantined_object_set_sha256": quarantine_set_sha,
        "quarantine_reasons": [quarantine_reason],
        "quarantine_details": [quarantine_detail],
        "failed_attempt_binding": failed_binding,
        "cycle1_duckdb_binding": cycle_binding,
        "deduplicated_overlapping_objects": 0,
        "overlap_keys": [],
        "manifest_object_set_sha256": manifest_set_sha,
        "selection_fingerprint_sha256": selection_sha,
        "outer_parser": "STRICT_NDJSON_IGNORE_ERRORS_FALSE; malformed outer rows abort",
    }
    rfq = {
        "schema": "sports-autoresearch-rfq-full-stage-v1",
        "run_id": run.name,
        "mode": mod.EXPECTED_MODE,
        "evidence": mod.EXPECTED_EVIDENCE,
        "status": mod.RFQ_PARTIAL_STATUS,
        "analysis_scope": mod.RFQ_ANALYSIS_SCOPE,
        "input": partial_input,
        "counts": {
            "valid_requests": 8,
            "observed_first_valid_deletes": 5,
            "right_censored_creates": 3,
        },
        "coverage": {
            "delete_endpoint_share": 0.625,
            "capture_completeness_is_not_lifecycle_join_completeness": True,
            "rfq_object_coverage": mod.RFQ_PARTIAL_STATUS,
            "quarantine_gap_count": 1,
            "quarantine_gap_ranges": [gap],
            "quarantine_gap_set_sha256": mod.canonical_json_sha256([gap]),
            "partial_object_coverage_hours": 1,
            "quarantined_hours_are_not_observed_zero": True,
        },
        "clob": {
            "causal_eligible_endpoint_anchors": 4,
            "matched_pairs": 2,
        },
        "hard_truth": {"broadcast_contains_accepted_quote_or_fill": False},
    }
    write_json(run / mod.RFQ_SUMMARY, rfq)
    write_json(
        run / mod.RFQ_INPUT_IDENTITY,
        {
            "schema": "rfq-full-input-identity-v2",
            "run_id": run.name,
            "releases": [
                {
                    "release_id": release_ids[0],
                    "manifest_sha256": mod.EXPECTED_RELEASE_BINDINGS[
                        release_ids[0]
                    ]["manifest_sha256"],
                    "rfq_objects": 1,
                    "rfq_bytes": 100,
                    "verified_marker_sha256": "1" * 64,
                },
                {
                    "release_id": release_ids[1],
                    "manifest_sha256": mod.EXPECTED_RELEASE_BINDINGS[
                        release_ids[1]
                    ]["manifest_sha256"],
                    "rfq_objects": 2,
                    "rfq_bytes": 150,
                    "verified_marker_sha256": "2" * 64,
                },
            ],
            **{
                key: value
                for key, value in partial_input.items()
                if key != "outer_parser"
            },
            "consumed_objects": consumed_rows,
        },
    )
    (run / mod.RFQ_QUARANTINE_GAPS).write_text(
        "release_id,key,sha256,previous_filename,next_filename,gap_start_ns,gap_end_ns,gap_start_us,gap_end_us,boundary_reason\n"
        f"{quarantine_release},{quarantine_key},{quarantine_sha},previous.ndjson,next.ndjson,1001,3001,1,4,WHOLE_OBJECT_QUARANTINE_MALFORMED_NDJSON\n",
        encoding="utf-8",
    )
    (run / mod.RFQ_HOUR_COVERAGE).write_text(
        "date,utc_hour,object_coverage_status,zero_interpretation\n"
        f"2026-07-14,0,{mod.RFQ_PARTIAL_STATUS},NOT_AN_OBSERVED_ZERO_QUARANTINE_OVERLAP\n",
        encoding="utf-8",
    )
    write_json(
        run / mod.ACTIVE_RFQ_STATE,
        {
            "schema": "rfq-full-stage-state-v1",
            "status": "COMPLETE_PARTIAL_OBJECT_COVERAGE_QUARANTINED",
            "phase": "COMPLETE",
            "resume": False,
            "input_fingerprint": selection_sha,
            "summary": mod.RFQ_SUMMARY.as_posix(),
            "completed_at_utc": "2026-07-15T14:00:00Z",
        },
    )
    write_json(
        run / mod.ACTIVE_RFQ_REPAIR_RESOURCE,
        {
            "schema_version": "w09-stage-resource-v1",
            "label": "rfq_full_stage_repair01",
            "return_code": 0,
            "command": [
                "python3",
                f"/srv/w09-research/runs/{run.name}/source/rfq_full_stage.py",
                "--run-dir",
                f"/srv/w09-research/runs/{run.name}",
            ],
        },
    )
    (run / "REPORT/tables/rfq_size_summary.csv").write_text(
        "contracts_n,target_n\n4,3\n", encoding="utf-8"
    )
    write_json(
        run / "REPORT/tables/L2_HYPOTHESIS_STAGE_SUMMARY.json",
        {
            "schema_version": "sports-autoresearch-l2-hypothesis-stage-v1",
            "stage": "L2_HYPOTHESIS_TESTS",
            "status": "COLLECT_MORE",
            "run_id": run.name,
            "banner": {
                "data_evidence": mod.EXPECTED_EVIDENCE,
                "timestamp_discipline": mod.EXPECTED_TIMESTAMP,
                "experiment_split": mod.EXPECTED_SPLIT,
                "artifact_status": "DIAGNOSTIC_ONLY",
                "authorization": mod.NO_LIVE_BANNER,
            },
            "data_binding": {
                "release_ids": list(mod.EXPECTED_RELEASE_BINDINGS)
            },
            "receipt_quality": {},
            "replay_qc": {},
            "hypotheses": {
                hypothesis_id: {
                    "hypothesis_id": hypothesis_id,
                    "hypothesis_status": "COLLECT_MORE",
                    "status_reason": "Only two degraded days.",
                }
                for hypothesis_id in mod.L2_TEST_IDS
            },
        },
    )
    write_json(
        run / "REPORT/tables/RFQ_TRIGGER_REPRODUCTION.json",
        {"run_id": run.name, "status": "TRIGGER_REPRODUCED"},
    )
    core_paths = (
        "REPORT/CYCLE1_CORE_SUMMARY.json",
        "REPORT/tables/CORE_HYPOTHESIS_TESTS.json",
        "REPORT/tables/L2_HYPOTHESIS_STAGE_SUMMARY.json",
        "REPORT/tables/RFQ_TRIGGER_REPRODUCTION.json",
    )
    core_inventory = [
        {
            "path": relative,
            "sha256": mod.sha256(run / relative),
            "bytes": (run / relative).stat().st_size,
        }
        for relative in core_paths
    ]
    archive_inventory = [
        {
            "path": archived.relative_to(pre_root).as_posix(),
            "sha256": mod.sha256(archived),
            "bytes": archived.stat().st_size,
        }
        for archived in sorted(pre_root.rglob("*"))
        if archived.is_file()
    ]
    manifest_path = run / "RUN_MANIFEST.json"
    registered_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    repair_record = registered_manifest["data_integrity_repairs"][0]
    repair_record["core_result_artifacts"] = core_inventory
    repair_record["archive_path"] = (
        "DATA_INTEGRITY/repairs/repair-01/pre_repair"
    )
    repair_record["archive_inventory"] = archive_inventory
    repair_record.pop("repair_receipt_path", None)
    repair_record.pop("repair_receipt_sha256", None)
    write_json(run / mod.REPAIR_REGISTRATION_RECEIPT, repair_record)
    repair_record["repair_receipt_path"] = mod.REPAIR_REGISTRATION_RECEIPT.as_posix()
    repair_record["repair_receipt_sha256"] = mod.sha256(
        run / mod.REPAIR_REGISTRATION_RECEIPT
    )
    write_json(manifest_path, registered_manifest)
    write_json(
        run / "DATA_COVERAGE.json",
        {
            "schema_version": "sports-autoresearch-coverage-v2",
            "coverage_status": "PARTIAL_CORE_FACT_CUBE_RFQ_FULL_STAGE_PENDING",
            "coverage_cube": "REPORT/tables/data_coverage_cube.csv",
            "coverage_cube_cells": 1,
            "channels": [
                {
                    "channel": "orderbooks_l1",
                    "included_rows": 1,
                    "n_markets": 1,
                    "n_games": 1,
                    "n_day_blocks": 1,
                }
            ],
            "exclusions": {"missing_receive_rows": 0},
            "limitations": [],
        },
    )
    usage_path = run / "RESOURCE_USAGE.json"
    write_json(
        usage_path,
        {
            "w09_driver_wall_seconds": 3600.0,
            "estimated_total_compute_cost_usd": 0.4713,
            "shutdown_confirmation": "PENDING_MISSION_END",
        },
    )
    manifest = json.loads((run / "RUN_MANIFEST.json").read_text(encoding="utf-8"))
    manifest["resource_usage"] = {
        "path": "RESOURCE_USAGE.json",
        "sha256": mod.sha256(usage_path),
        "estimated_total_compute_cost_usd": 0.4713,
    }
    write_json(run / "RUN_MANIFEST.json", manifest)
    return run


def test_finalizer_is_terminal_idempotent_and_self_contained(tmp_path):
    run = make_run(tmp_path)
    mod.finalize(run)

    manifest = json.loads((run / "RUN_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "COMPLETE"
    assert manifest["terminal_condition"]["id"] == 4
    summary = json.loads(
        (run / "REPORT/tables/FINAL_MISSION_SUMMARY.json").read_text(encoding="utf-8")
    )
    assert summary["hypotheses_tested"] == 10
    assert summary["promotion_ready"] == 0
    assert summary["formal_verdict_pass"] == 0
    assert summary["shortlisted_strategies"] == []
    assert summary["status_counts"]["COLLECT_MORE"] == 7
    assert summary["status_counts"]["DATA_STARVED"] == 3
    full = (run / "REPORT/FULL_REPORT.md").read_text(encoding="utf-8")
    assert full.count("\n## ") == 25
    page = (run / "REPORT/index.html").read_text(encoding="utf-8")
    assert "data:image/png;base64," in page
    assert "<style>" in page and "<script>" in page
    rfq = json.loads((run / mod.RFQ_CONCLUSIONS).read_text(encoding="utf-8"))
    assert {
        item["hypothesis_status"] for item in rfq["hypotheses"].values()
    } == {"COLLECT_MORE"}
    assert rfq["coverage_status"] == mod.RFQ_PARTIAL_STATUS
    assert rfq["whole_object_quarantine"] is True
    assert rfq["line_salvage"] is False
    for result in rfq["hypotheses"].values():
        assert "retained observed subset" in result["status_reason"].lower()
        assert "temporal-selection bias" in result["status_reason"].lower()
        assert "structurally valid immutable replacement" in result[
            "reopen_condition"
        ].lower()
    coverage = json.loads((run / "DATA_COVERAGE.json").read_text(encoding="utf-8"))
    assert coverage["coverage_status"].startswith("PARTIAL_")
    assert "COMPLETE_MODE1" not in coverage["coverage_status"]
    assert coverage["stage_completeness"]["rfq_full_stage"]["status"] == (
        mod.RFQ_PARTIAL_STATUS
    )
    rfq_coverage = coverage["stage_completeness"]["rfq_full_stage"]
    assert rfq_coverage["line_salvage"] is False
    assert all(
        rfq_coverage[key] is not None
        for key in (
            "unique_objects_total",
            "retained_unique_objects",
            "quarantined_unique_objects",
            "unique_bytes_total",
            "retained_bytes",
            "quarantined_bytes",
        )
    )
    assert rfq_coverage["retained_unique_objects"] + rfq_coverage[
        "quarantined_unique_objects"
    ] == rfq_coverage["unique_objects_total"]
    assert rfq_coverage["retained_bytes"] + rfq_coverage[
        "quarantined_bytes"
    ] == rfq_coverage["unique_bytes_total"]
    required_boundary_outputs = (
        "REPORT/FULL_REPORT.md",
        "REPORT/EXECUTIVE_SUMMARY.md",
        "REPORT/index.html",
        "REPORT/tables/FINAL_MISSION_SUMMARY.json",
        str(mod.RFQ_CONCLUSIONS),
        "REPRODUCE.md",
    )
    for relative in required_boundary_outputs:
        output = (run / relative).read_text(encoding="utf-8").lower()
        assert "retained observed subset" in output, relative
        assert "temporal-selection bias" in output, relative
        assert "line salvage" in output, relative
        assert "reopen" in output, relative
    reproduce = (run / "REPRODUCE.md").read_text(encoding="utf-8")
    for expected in (
        "initial execution commit",
        "previous execution commit",
        "current repair execution commit",
        str(mod.RFQ_DECLARATION),
        str(mod.RFQ_MALFORMED_RECEIPT),
        str(mod.FAILED_RFQ_STATE),
        str(mod.FAILED_RFQ_RESOURCE),
        str(mod.FAILED_RFQ_SCRATCH_RECEIPT),
        str(mod.REPAIR_REGISTRATION_RECEIPT),
        str(mod.ACTIVE_RFQ_STATE),
        str(mod.ACTIVE_RFQ_REPAIR_RESOURCE),
        "source checksum-set SHA-256",
        "quarantined object-set SHA-256",
        "quarantine gap-set SHA-256",
    ):
        assert expected in reproduce
    registry_before = (run / "TRIAL_REGISTRY.jsonl").read_bytes()
    sums_before = (run / "ARTIFACT_SHA256SUMS").read_bytes()

    # Re-running completion must not append a second final result or change
    # deterministic artifacts.
    mod.finalize(run)
    assert (run / "TRIAL_REGISTRY.jsonl").read_bytes() == registry_before
    assert (run / "ARTIFACT_SHA256SUMS").read_bytes() == sums_before
    mod.verify_artifacts(run)


def test_completed_archive_rerun_refuses_to_reseal_tampering(tmp_path):
    run = make_run(tmp_path)
    mod.finalize(run)
    registry_path = run / "TRIAL_REGISTRY.jsonl"
    records = registry_path.read_text(encoding="utf-8").splitlines()
    last = json.loads(records[-1])
    last["status_reason"] = "post-completion mutation"
    records[-1] = json.dumps(last, sort_keys=True)
    registry_path.write_text("\n".join(records) + "\n", encoding="utf-8")

    with pytest.raises(mod.MissionFinalizationError, match="artifact hash mismatch"):
        mod.finalize(run)


def test_finalizer_requires_resource_finalize_hash_binding(tmp_path):
    run = make_run(tmp_path)
    usage_path = run / "RESOURCE_USAGE.json"
    usage = json.loads(usage_path.read_text(encoding="utf-8"))
    usage["estimated_total_compute_cost_usd"] = 999.0
    write_json(usage_path, usage)

    with pytest.raises(mod.MissionFinalizationError, match="not bound"):
        mod.finalize(run)


@pytest.mark.parametrize(
    "mutation",
    [
        "top_status",
        "missing_analysis_scope",
        "missing_line_salvage",
        "forged_full_coverage",
        "forged_object_set_hash",
        "coordinated_silent_omission",
        "count_byte_mismatch",
        "quarantine_detail_version",
        "gap_set_hash",
        "gap_boundary",
        "failure_binding",
        "repair_previous_commit",
        "repair_failure_hash",
        "receipt_object_sha",
        "missing_repair",
        "active_state_incomplete",
        "active_state_fingerprint",
        "successful_resource_failed",
        "successful_resource_resume",
        "successful_resource_wrong_source",
        "successful_resource_relative_source",
        "repair_receipt_mutation",
        "current_source_sums_mutation",
        "archive_file_mutation",
        "preserved_core_result_mutation",
        "failed_input_identity_mutation",
        "cycle1_receipt_mutation",
        "cycle1_database_mutation",
        "input_identity_wrong_run_id",
        "input_identity_missing_run_id",
    ],
)
def test_rfq_partial_quarantine_mutations_fail_closed(tmp_path, mutation):
    run = make_run(tmp_path)
    rfq_path = run / mod.RFQ_SUMMARY
    manifest_path = run / "RUN_MANIFEST.json"
    rfq = json.loads(rfq_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if mutation == "top_status":
        rfq["status"] = "DESCRIPTIVE_DISCOVERY_ONLY"
        write_json(rfq_path, rfq)
    elif mutation == "missing_analysis_scope":
        rfq.pop("analysis_scope")
        write_json(rfq_path, rfq)
    elif mutation == "missing_line_salvage":
        rfq["input"].pop("line_salvage")
        write_json(rfq_path, rfq)
    elif mutation == "forged_full_coverage":
        rfq["input"]["full_object_coverage"] = True
        write_json(rfq_path, rfq)
    elif mutation == "forged_object_set_hash":
        rfq["input"]["quarantined_object_set_sha256"] = "0" * 64
        write_json(rfq_path, rfq)
    elif mutation == "coordinated_silent_omission":
        identity_path = run / mod.RFQ_INPUT_IDENTITY
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        retained = identity["consumed_objects"][:1]
        quarantined = json.loads(
            (run / mod.RFQ_DECLARATION).read_text(encoding="utf-8")
        )["quarantined_objects"]
        consumed_hash = mod.object_set_sha256(retained)
        total_hash = mod.object_set_sha256([*retained, *quarantined])
        selection_hash = mod.canonical_json_sha256(
            {
                "total": total_hash,
                "consumed": consumed_hash,
                "quarantined": rfq["input"]["quarantined_object_set_sha256"],
                "receipt": mod.sha256(run / mod.RFQ_MALFORMED_RECEIPT),
                "declaration": mod.sha256(run / mod.RFQ_DECLARATION),
            }
        )
        coordinated = {
            "logical_manifest_bindings_total": 2,
            "unique_objects_total": 2,
            "unique_bytes_total": 150,
            "consumed_unique_objects": 1,
            "consumed_logical_bindings": 1,
            "consumed_bytes": 100,
            "consumed_object_set_sha256": consumed_hash,
            "manifest_object_set_sha256": total_hash,
            "selection_fingerprint_sha256": selection_hash,
        }
        rfq["input"].update(coordinated)
        identity.update(coordinated)
        identity["consumed_objects"] = retained
        write_json(rfq_path, rfq)
        write_json(identity_path, identity)
    elif mutation == "count_byte_mismatch":
        rfq["input"]["consumed_bytes"] += 1
        write_json(rfq_path, rfq)
    elif mutation == "quarantine_detail_version":
        rfq["input"]["quarantine_details"][0]["version_id"] = "forged"
        write_json(rfq_path, rfq)
    elif mutation == "gap_set_hash":
        rfq["coverage"]["quarantine_gap_set_sha256"] = "0" * 64
        write_json(rfq_path, rfq)
    elif mutation == "gap_boundary":
        rfq["coverage"]["quarantine_gap_ranges"][0]["gap_end_us"] += 1
        write_json(rfq_path, rfq)
    elif mutation == "failure_binding":
        rfq["input"]["failed_attempt_binding"]["failed_state_sha256"] = "0" * 64
        write_json(rfq_path, rfq)
    elif mutation == "repair_previous_commit":
        manifest["data_integrity_repairs"][0]["previous_execution_commit"] = "0" * 40
        write_json(manifest_path, manifest)
    elif mutation == "repair_failure_hash":
        manifest["data_integrity_repairs"][0]["failed_state_sha256"] = "0" * 64
        write_json(manifest_path, manifest)
    elif mutation == "receipt_object_sha":
        receipt_path = run / mod.RFQ_MALFORMED_RECEIPT
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["observed_sha256"] = "0" * 64
        write_json(receipt_path, receipt)
    elif mutation == "active_state_incomplete":
        state_path = run / mod.ACTIVE_RFQ_STATE
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["status"] = "RUNNING"
        write_json(state_path, state)
    elif mutation == "active_state_fingerprint":
        state_path = run / mod.ACTIVE_RFQ_STATE
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["input_fingerprint"] = "0" * 64
        write_json(state_path, state)
    elif mutation == "successful_resource_failed":
        resource_path = run / mod.ACTIVE_RFQ_REPAIR_RESOURCE
        resource = json.loads(resource_path.read_text(encoding="utf-8"))
        resource["return_code"] = 1
        write_json(resource_path, resource)
    elif mutation == "successful_resource_resume":
        resource_path = run / mod.ACTIVE_RFQ_REPAIR_RESOURCE
        resource = json.loads(resource_path.read_text(encoding="utf-8"))
        resource["command"].append("--resume")
        write_json(resource_path, resource)
    elif mutation == "successful_resource_wrong_source":
        resource_path = run / mod.ACTIVE_RFQ_REPAIR_RESOURCE
        resource = json.loads(resource_path.read_text(encoding="utf-8"))
        resource["command"][1] = "/tmp/source/not_the_repair.py"
        write_json(resource_path, resource)
    elif mutation == "successful_resource_relative_source":
        resource_path = run / mod.ACTIVE_RFQ_REPAIR_RESOURCE
        resource = json.loads(resource_path.read_text(encoding="utf-8"))
        resource["command"][1] = f"{run.name}/source/rfq_full_stage.py"
        write_json(resource_path, resource)
    elif mutation == "repair_receipt_mutation":
        receipt_path = run / mod.REPAIR_REGISTRATION_RECEIPT
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["rfq_result_state"] = "FORGED_RESULT_OPENED"
        write_json(receipt_path, receipt)
    elif mutation == "current_source_sums_mutation":
        source_sums = run / "SOURCE_SHA256SUMS.txt"
        source_sums.write_text(
            source_sums.read_text(encoding="utf-8") + "0" * 64 + "  forged\n",
            encoding="utf-8",
        )
    elif mutation == "archive_file_mutation":
        archived_query = (
            run
            / "DATA_INTEGRITY/repairs/repair-01/pre_repair/queries/"
            "rfq_full_stage.py"
        )
        archived_query.write_text("# post-registration archive mutation\n", encoding="utf-8")
    elif mutation == "preserved_core_result_mutation":
        trigger = run / "REPORT/tables/RFQ_TRIGGER_REPRODUCTION.json"
        value = json.loads(trigger.read_text(encoding="utf-8"))
        value["status"] = "FORGED"
        write_json(trigger, value)
    elif mutation == "failed_input_identity_mutation":
        identity_path = run / mod.FAILED_RFQ_INPUT_IDENTITY
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        identity["objects"] = identity["objects"][:-1]
        write_json(identity_path, identity)
    elif mutation == "cycle1_receipt_mutation":
        cycle_path = run / mod.ACTIVE_CYCLE1_DUCKDB_BINDING
        cycle = json.loads(cycle_path.read_text(encoding="utf-8"))
        cycle["duckdb_version"] = "9.9.9"
        write_json(cycle_path, cycle)
    elif mutation == "cycle1_database_mutation":
        (run / "cache/cycle1.duckdb").write_bytes(b"forged-cycle-db")
    elif mutation == "input_identity_wrong_run_id":
        identity_path = run / mod.RFQ_INPUT_IDENTITY
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        identity["run_id"] = "wrong-run-id"
        write_json(identity_path, identity)
    elif mutation == "input_identity_missing_run_id":
        identity_path = run / mod.RFQ_INPUT_IDENTITY
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        identity.pop("run_id")
        write_json(identity_path, identity)
    else:
        manifest["data_integrity_repairs"] = []
        write_json(manifest_path, manifest)

    with pytest.raises(mod.MissionFinalizationError):
        mod.finalize(run)


@pytest.mark.parametrize("mutation", ["summary", "resource", "release"])
def test_verify_recomputes_manifest_internal_bindings(tmp_path, mutation):
    run = make_run(tmp_path)
    mod.finalize(run)
    manifest_path = run / "RUN_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if mutation == "summary":
        manifest["finalization"]["summary_sha256"] = "0" * 64
    elif mutation == "resource":
        manifest["resource_usage"]["sha256"] = "0" * 64
    else:
        manifest["selected_releases"][0]["manifest_sha256"] = "0" * 64
    write_json(manifest_path, manifest)

    with pytest.raises(mod.MissionFinalizationError, match="binding mismatch"):
        mod.verify_artifacts(run)


def test_finalizer_rejects_forbidden_l2_status_and_rfq_zero_is_data_starved(tmp_path):
    run = make_run(tmp_path)
    rfq_path = run / mod.RFQ_SUMMARY
    rfq = json.loads(rfq_path.read_text(encoding="utf-8"))
    rfq["clob"]["matched_pairs"] = 0
    write_json(rfq_path, rfq)
    manifest = json.loads((run / "RUN_MANIFEST.json").read_text(encoding="utf-8"))
    quarantine = mod.validate_rfq_partial_quarantine(run, manifest, rfq)
    conclusions = mod.build_rfq_conclusions(
        run,
        manifest,
        rfq,
        quarantine,
        completed_at_utc="2026-07-15T12:00:00Z",
    )
    assert conclusions["hypotheses"]["C1-RFQ-CLOB-01"]["hypothesis_status"] == "DATA_STARVED"
    assert conclusions["hypotheses"]["C1-ANOM-RFQ-SIZE-TAIL-01"]["hypothesis_status"] == "COLLECT_MORE"

    frozen = run / "queries/finalize_mission.py"
    original = frozen.read_bytes()
    frozen.write_bytes(original + b"\n# post-freeze mutation\n")
    with pytest.raises(mod.MissionFinalizationError, match="frozen query hash mismatch"):
        mod.validate_frozen_query_source(run, manifest)
    frozen.write_bytes(original)

    l2_path = run / "REPORT/tables/L2_HYPOTHESIS_STAGE_SUMMARY.json"
    l2 = json.loads(l2_path.read_text(encoding="utf-8"))
    l2["hypotheses"]["C1-HFOLLOW-RETREAT-01"]["hypothesis_status"] = "PROMOTION_READY"
    write_json(l2_path, l2)
    with pytest.raises(mod.MissionFinalizationError, match="preserved core-result artifact changed"):
        mod.finalize(run)


def test_repair02_selection_fingerprint_contract_is_exact():
    payload = {
        "schema": "rfq-partial-object-selection-v2",
        "total": mod.EXPECTED_RFQ_FULL_SET_SHA256,
        "retained": mod.EXPECTED_RFQ_RETAINED_SET_SHA256,
        "quarantined": mod.EXPECTED_RFQ_QUARANTINED_SET_SHA256,
        "declaration": (
            "f1311f2824742f7d2da6ba3416b802b0fa5a844642f1e631f566cc2a5e1ec07d"
        ),
        "receipts": [
            "dca39e5781578fd667b83eaa6286d6b7762b2fa22404fe89c55a66409619a4ac",
            "7890f86055b6c730a15acdbfee776496de5b8bb86cf55195f66f882ffb661b90",
        ],
    }
    assert mod.canonical_json_sha256(payload) == (
        mod.EXPECTED_RFQ_REPAIR02_SELECTION_SHA256
    )


def test_repair02_boundary_count_distinguishes_quarantine_from_other_boundaries():
    coverage = {
        "quarantine_gap_count": 1,
        "quarantined_object_count": 2,
        "quarantine_observation_boundary_count": 2,
    }
    # Loss/error/epoch boundaries can increase the global count; the quarantine
    # contribution remains exactly two timestamps for the one contiguous gap.
    mod._validate_rfq_quarantine_boundary_counts(
        coverage, {"observation_boundary_timestamps": 47}
    )


def repair02_success_envelope_fixture():
    repair_chain = [
        {"repair_id": "repair-01", "registration_sha256": "1" * 64},
        {"repair_id": "repair-02", "registration_sha256": "2" * 64},
    ]
    failed_bindings = [
        {"repair_id": "repair-01", "attempt_id": "RFQ_FULL_STAGE_ATTEMPT_01"},
        {"repair_id": "repair-02", "attempt_id": "RFQ_FULL_STAGE_REPAIR01_ATTEMPT_02"},
    ]
    gap_plan = {
        "quarantined_object_count": 2,
        "contiguous_gap_count": 1,
        "observation_boundary_count": 2,
        "contiguous_runs": [{
            "anchor_key": "raw_rfq/date=2026-07-13/rfq_23.ndjson.2",
            "quarantined_keys": [
                "raw_rfq/date=2026-07-13/rfq_23.ndjson.2",
                "raw_rfq/date=2026-07-14/rfq_00.ndjson",
            ],
        }],
    }
    resource = {
        "label": "rfq_full_stage_repair02",
        "path": "logs/resources/rfq_full_stage_repair02.json",
    }
    selection = mod.EXPECTED_RFQ_REPAIR02_SELECTION_SHA256
    summary_expected = {
        "coverage_status": mod.RFQ_PARTIAL_STATUS,
        "consumed_unique_objects": 282,
        "quarantined_unique_objects": 2,
        "line_salvage": False,
        "repair_chain": repair_chain,
        "failed_attempt_bindings": failed_bindings,
        "quarantine_gap_plan": gap_plan,
        "expected_success_resource": resource,
        "selection_fingerprint_sha256": selection,
    }
    identity_expected = {
        "schema": "rfq-full-input-identity-v2",
        "run_id": "synthetic-repair02",
        **summary_expected,
        "consumed_objects": [{"key": "retained"}],
    }
    state = {
        "schema": "rfq-full-stage-state-v1",
        "run_id": "synthetic-repair02",
        "status": "COMPLETE_PARTIAL_OBJECT_COVERAGE_QUARANTINED",
        "phase": "COMPLETE",
        "resume": False,
        "input_fingerprint": selection,
        "summary": mod.RFQ_SUMMARY.as_posix(),
        "repair_chain": repair_chain,
        "failed_attempt_bindings": failed_bindings,
        "quarantine_gap_plan": gap_plan,
        "expected_success_resource": resource,
        "completed_at_utc": "2026-07-15T18:00:00Z",
    }
    return {
        "rfq_input": dict(summary_expected),
        "active_identity": dict(identity_expected),
        "active_state": state,
        "summary_expected": summary_expected,
        "identity_expected": identity_expected,
        "run_id": "synthetic-repair02",
        "selection_sha": selection,
        "repair_chain": repair_chain,
        "failed_bindings": failed_bindings,
        "gap_plan": gap_plan,
        "expected_resource": resource,
    }


def test_repair02_synthetic_success_envelope_is_exact():
    mod._validate_repair02_success_envelope(**repair02_success_envelope_fixture())


@pytest.mark.parametrize("mutation", ["summary", "identity", "state"])
def test_repair02_synthetic_success_mutations_fail_closed(mutation):
    fixture = repair02_success_envelope_fixture()
    if mutation == "summary":
        fixture["rfq_input"]["failed_attempt_bindings"] = []
    elif mutation == "identity":
        fixture["active_identity"]["consumed_objects"] = []
    else:
        fixture["active_state"]["quarantine_gap_plan"] = {
            "quarantined_object_count": 2,
            "contiguous_gap_count": 2,
            "observation_boundary_count": 4,
            "contiguous_runs": [],
        }
    with pytest.raises(mod.MissionFinalizationError):
        mod._validate_repair02_success_envelope(**fixture)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quarantine_gap_count", 2),
        ("quarantined_object_count", 1),
        ("quarantine_observation_boundary_count", 4),
    ],
)
def test_repair02_quarantine_boundary_mutations_fail_closed(field, value):
    coverage = {
        "quarantine_gap_count": 1,
        "quarantined_object_count": 2,
        "quarantine_observation_boundary_count": 2,
    }
    coverage[field] = value
    with pytest.raises(mod.MissionFinalizationError, match="quarantine boundaries"):
        mod._validate_rfq_quarantine_boundary_counts(
            coverage, {"observation_boundary_timestamps": 9}
        )


def test_repair02_manifest_status_reaches_frozen_source_validation(tmp_path, monkeypatch):
    class ReachedFrozenSource(Exception):
        pass

    manifest = {
        "status": mod.REPAIR02_PENDING_STATUS,
        "data_integrity_repairs": [
            {"repair_id": "repair-01"},
            {"repair_id": "repair-02"},
        ],
    }
    monkeypatch.setattr(mod, "validate_run_identity", lambda *_: (manifest, {}))
    monkeypatch.setattr(
        mod,
        "validate_frozen_query_source",
        lambda *_: (_ for _ in ()).throw(ReachedFrozenSource()),
    )
    with pytest.raises(ReachedFrozenSource):
        mod.finalize(tmp_path)


def test_repair02_dispatches_to_double_quarantine_validator(monkeypatch, tmp_path):
    expected = {"status": "double-validated"}
    monkeypatch.setattr(
        mod, "validate_rfq_double_quarantine", lambda *_: expected
    )
    manifest = {
        "data_integrity_repairs": [
            {"repair_id": "repair-01"},
            {"repair_id": "repair-02"},
        ]
    }
    assert mod.validate_rfq_partial_quarantine(tmp_path, manifest, {}) is expected


def repair03_parser_contract_fixture():
    return {
        "schema_version": "rfq-inner-payload-parser-contract-v1",
        "run_id": "synthetic-repair03",
        "created_at_utc": "2026-07-15T16:00:00Z",
        "finding": "EXPECTED_CONTROL_MARKER_MISCLASSIFIED_AS_MALFORMED_INNER_PAYLOAD",
        "mission_sha256": mod.EXPECTED_MISSION_SHA,
        "selection_fingerprint_sha256": mod.EXPECTED_RFQ_REPAIR02_SELECTION_SHA256,
        "retained_unique_objects": mod.EXPECTED_RFQ_RETAINED_OBJECTS,
        "retained_bytes": mod.EXPECTED_RFQ_RETAINED_BYTES,
        "outer_ndjson_policy": "STRICT_NDJSON_IGNORE_ERRORS_FALSE",
        "expected_blank_control_markers": list(
            mod.EXPECTED_RFQ_BLANK_CONTROL_MARKERS
        ),
        "expected_blank_raw_representation": "EXACT_EMPTY_STRING",
        "non_control_payload_policy": "NONEMPTY_STRING_VALID_JSON_OBJECT_REQUIRED",
        "unexpected_payload_policy": "ABORT_BEFORE_RESULT",
        "line_salvage": False,
        "data_selection_change": False,
        "hypothesis_design_change": False,
        "registered_query_path": "queries/rfq_full_stage.py",
        "registered_query_sha256": "a" * 64,
        "audit_path": mod.ARCHIVED_RFQ_INNER_PAYLOAD_AUDIT_03.as_posix(),
        "audit_sha256": "b" * 64,
    }


def test_repair03_parser_contract_is_exact():
    contract = repair03_parser_contract_fixture()
    mod._validate_repair03_parser_contract(
        contract,
        run_id="synthetic-repair03",
        registered_query_sha256="a" * 64,
        audit_path=mod.ARCHIVED_RFQ_INNER_PAYLOAD_AUDIT_03,
        audit_sha256="b" * 64,
    )


@pytest.mark.parametrize(
    "field",
    [
        "expected_blank_control_markers",
        "non_control_payload_policy",
        "line_salvage",
        "data_selection_change",
        "registered_query_sha256",
        "audit_sha256",
    ],
)
def test_repair03_parser_contract_mutations_fail_closed(field):
    contract = repair03_parser_contract_fixture()
    if isinstance(contract[field], bool):
        contract[field] = not contract[field]
    elif isinstance(contract[field], list):
        contract[field] = contract[field][1:]
    else:
        contract[field] = "forged"
    with pytest.raises(mod.MissionFinalizationError, match="parser-contract"):
        mod._validate_repair03_parser_contract(
            contract,
            run_id="synthetic-repair03",
            registered_query_sha256="a" * 64,
            audit_path=mod.ARCHIVED_RFQ_INNER_PAYLOAD_AUDIT_03,
            audit_sha256="b" * 64,
        )


def repair03_audit_fixture():
    contract = {
        "schema_version": "rfq-inner-payload-parser-contract-v1",
        "marker_case": "EXACT_CASE_SENSITIVE",
        "data_frame_marker": "MARKER_FIELD_ABSENT",
        "data_frame_raw": "NONEMPTY_JSON_OBJECT",
        "json_object_payload_markers": ["segment_receipt"],
        "empty_control_markers": list(mod.EXPECTED_RFQ_BLANK_CONTROL_MARKERS),
        "empty_control_raw": "EXACT_EMPTY_STRING",
        "explicit_null_marker": "FAIL_CLOSED",
        "unknown_or_invalid_marker": "FAIL_CLOSED",
        "missing_null_or_non_string_raw": "FAIL_CLOSED",
        "valid_non_object_inner_json": "FAIL_CLOSED",
        "raw_b64_without_raw": "FAIL_CLOSED",
    }
    rows = []
    remaining_bytes = mod.EXPECTED_RFQ_RETAINED_BYTES
    remaining_lines = mod.EXPECTED_RFQ_RETAINED_LINES
    for index in range(mod.EXPECTED_RFQ_RETAINED_OBJECTS):
        last = index == mod.EXPECTED_RFQ_RETAINED_OBJECTS - 1
        size = remaining_bytes if last else 1
        lines = remaining_lines if last else 1
        remaining_bytes -= size
        remaining_lines -= lines
        rows.append(
            {
                "key": f"raw_rfq/date=2026-07-13/rfq_{index:03d}.ndjson",
                "expected_size": size,
                "observed_size": size,
                "expected_sha256": f"{index:064x}",
                "observed_sha256": f"{index:064x}",
                "identity_match": True,
                "total_lines": lines,
                "outer_invalid_rows": 0,
                "expected_empty_control_marker_rows": (
                    mod.EXPECTED_RFQ_EMPTY_CONTROL_ROWS if index == 0 else 0
                ),
                "data_frame_rows": 58_143_241 if index == 0 else 0,
                "segment_receipt_rows": 560 if index == 0 else 0,
                "marker_counts": (
                    {
                        **mod.EXPECTED_RFQ_CONTROL_MARKER_COUNTS,
                        "segment_receipt": 560,
                    }
                    if index == 0
                    else {}
                ),
                "unexpected_inner_payload_rows": 0,
                "unexpected_details_capped_at": 100,
                "unexpected_details": [],
                "raw_payload_redacted": True,
            }
        )
    return {
        "schema_version": "rfq-inner-payload-contract-audit-v1",
        "run_id": "synthetic-repair03",
        "started_at_utc": "2026-07-15T15:30:00Z",
        "completed_at_utc": "2026-07-15T15:35:00Z",
        "wall_seconds": 300.0,
        "scope": "RETAINED_OBJECT_INNER_PAYLOAD_CONTRACT_ONLY_NO_RESEARCH_RESULT",
        "analysis_result_opened": False,
        "raw_payload_redacted": True,
        "workers": 8,
        "input_identity_path": mod.RFQ_INPUT_IDENTITY.as_posix(),
        "input_identity_sha256": "c" * 64,
        "input_fingerprint": mod.EXPECTED_RFQ_REPAIR02_SELECTION_SHA256,
        "audit_script_path": "tmp/rfq_inner_payload_contract_audit03.py",
        "audit_script_sha256": "d" * 64,
        "parser_contract": contract,
        "parser_contract_sha256": mod.canonical_json_sha256(contract),
        "expected_empty_control_marker_allowlist": list(
            mod.EXPECTED_RFQ_BLANK_CONTROL_MARKERS
        ),
        "json_object_payload_marker_contract": [
            "<marker field absent>", "segment_receipt"
        ],
        "objects_scanned": mod.EXPECTED_RFQ_RETAINED_OBJECTS,
        "bytes_scanned": mod.EXPECTED_RFQ_RETAINED_BYTES,
        "lines_scanned": mod.EXPECTED_RFQ_RETAINED_LINES,
        "identity_mismatch_count": 0,
        "identity_mismatches": [],
        "outer_invalid_object_count": 0,
        "outer_invalid_line_count": 0,
        "expected_empty_control_marker_rows": mod.EXPECTED_RFQ_EMPTY_CONTROL_ROWS,
        "data_frame_rows": 58_143_241,
        "segment_receipt_rows": 560,
        "marker_counts": {
            **mod.EXPECTED_RFQ_CONTROL_MARKER_COUNTS,
            "segment_receipt": 560,
        },
        "unexpected_inner_payload_object_count": 0,
        "unexpected_inner_payload_row_count": 0,
        "unexpected_inner_payload_objects": [],
        "object_summaries": rows,
        "status": "COMPLETE_INNER_PAYLOAD_CONTRACT_AUDIT",
    }


def test_repair03_inner_payload_audit_reconciles_all_object_totals():
    audit = repair03_audit_fixture()
    expected_objects = [
        {
            "key": row["key"],
            "size": row["expected_size"],
            "sha256": row["expected_sha256"],
        }
        for row in audit["object_summaries"]
    ]
    mod._validate_repair03_inner_payload_audit(
        audit,
        run_id="synthetic-repair03",
        input_identity_sha256="c" * 64,
        audit_source_sha256="d" * 64,
        expected_objects=expected_objects,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "unexpected",
        "marker_count",
        "object_identity",
        "object_bytes",
        "object_key",
        "input",
    ],
)
def test_repair03_inner_payload_audit_mutations_fail_closed(mutation):
    audit = repair03_audit_fixture()
    expected_objects = [
        {
            "key": row["key"],
            "size": row["expected_size"],
            "sha256": row["expected_sha256"],
        }
        for row in audit["object_summaries"]
    ]
    if mutation == "unexpected":
        audit["unexpected_inner_payload_row_count"] = 1
    elif mutation == "marker_count":
        audit["marker_counts"]["hour_open"] -= 1
    elif mutation == "object_identity":
        audit["object_summaries"][0]["identity_match"] = False
    elif mutation == "object_bytes":
        audit["object_summaries"][0]["observed_size"] += 1
    elif mutation == "object_key":
        audit["object_summaries"][0]["key"] = (
            "raw_rfq/date=2026-07-13/forged.ndjson"
        )
    else:
        audit["input_identity_sha256"] = "0" * 64
    with pytest.raises(mod.MissionFinalizationError):
        mod._validate_repair03_inner_payload_audit(
            audit,
            run_id="synthetic-repair03",
            input_identity_sha256="c" * 64,
            audit_source_sha256="d" * 64,
            expected_objects=expected_objects,
        )


def repair03_audit_resource_fixture():
    return {
        "schema_version": "w09-stage-resource-v1",
        "label": "rfq_inner_payload_contract_audit03_final",
        "return_code": 0,
        "command": [
            "/opt/w09/venv/bin/python",
            (
                "/srv/w09-research/runs/synthetic-repair03/"
                "tmp/rfq_inner_payload_contract_audit03.py"
            ),
            "--run-dir",
            "/srv/w09-research/runs/synthetic-repair03",
            "--cache-root",
            "/srv/w09-research/cache",
            "--workers",
            "8",
        ],
    }


def test_repair03_audit_resource_is_exact():
    mod._validate_repair03_audit_resource(
        repair03_audit_resource_fixture(), run_id="synthetic-repair03"
    )


@pytest.mark.parametrize("mutation", ["label", "source", "workers"])
def test_repair03_audit_resource_mutations_fail_closed(mutation):
    resource = repair03_audit_resource_fixture()
    if mutation == "label":
        resource["label"] = "rfq_inner_payload_contract_audit03"
    elif mutation == "source":
        resource["command"][1] = "/tmp/forged.py"
    else:
        resource["command"][-1] = "16"
    with pytest.raises(mod.MissionFinalizationError, match="audit resource"):
        mod._validate_repair03_audit_resource(
            resource, run_id="synthetic-repair03"
        )


def repair03_snapshot_fixture(tmp_path):
    run = tmp_path / "synthetic-repair03"
    previous = {
        "execution_commit": "1" * 40,
        "source_manifest_sha256": "2" * 64,
        "source_sha256s_sha256": "3" * 64,
    }
    current = {
        "execution_commit": "4" * 40,
        "source_manifest_sha256": "5" * 64,
        "source_sha256s_sha256": "6" * 64,
    }
    attestation = {
        "schema_version": "sports-autoresearch-source-snapshot-attestation-v1",
        "run_id": run.name,
        "mission_sha256": mod.EXPECTED_MISSION_SHA,
        "parent_execution_commit": previous["execution_commit"],
        "execution_commit": current["execution_commit"],
        "direct_parent_verified": True,
        "git_tree": "7" * 40,
        "source_relative": mod.REPAIR_03_SOURCE_RELATIVE,
        "source_tree_clean_at_attestation": True,
        "source_manifest_sha256": current["source_manifest_sha256"],
        "source_sha256s_sha256": current["source_sha256s_sha256"],
        "commit_changed_paths": mod.EXPECTED_REPAIR_03_CHANGED_PATHS,
        "attested_at_utc": "2026-07-15T16:00:00Z",
    }
    active = run / mod.REPAIR_03_SOURCE_ATTESTATION_ACTIVE
    archived = run / mod.REPAIR_03_SOURCE_ATTESTATION_ARCHIVE
    write_json(active, attestation)
    archived.parent.mkdir(parents=True, exist_ok=True)
    archived.write_bytes(active.read_bytes())
    attestation_sha = mod.sha256(active)
    repair = {
        "source_verification_mode": mod.REPAIR_03_SNAPSHOT_SOURCE_MODE,
        "source_snapshot_attestation_active_path": (
            mod.REPAIR_03_SOURCE_ATTESTATION_ACTIVE.as_posix()
        ),
        "source_snapshot_attestation_path": (
            mod.REPAIR_03_SOURCE_ATTESTATION_ARCHIVE.as_posix()
        ),
        "source_snapshot_attestation_sha256": attestation_sha,
        "source_snapshot_attestation": {
            "schema_version": attestation["schema_version"],
            "execution_commit": current["execution_commit"],
            "git_tree": attestation["git_tree"],
            "direct_parent_verified": True,
            "source_relative": mod.REPAIR_03_SOURCE_RELATIVE,
            "source_tree_clean_at_attestation": True,
            "commit_changed_paths": mod.EXPECTED_REPAIR_03_CHANGED_PATHS,
        },
    }
    return run, repair, previous, current


def test_repair03_snapshot_source_verification_is_exact(tmp_path):
    run, repair, previous, current = repair03_snapshot_fixture(tmp_path)
    mod._validate_repair03_source_verification(
        run,
        run_id=run.name,
        repair03=repair,
        previous_identity=previous,
        current_identity=current,
    )


@pytest.mark.parametrize("mutation", ["archive", "parent", "nested", "paths"])
def test_repair03_snapshot_source_mutations_fail_closed(tmp_path, mutation):
    run, repair, previous, current = repair03_snapshot_fixture(tmp_path)
    if mutation == "archive":
        (run / mod.REPAIR_03_SOURCE_ATTESTATION_ARCHIVE).write_text(
            "forged", encoding="utf-8"
        )
    elif mutation == "parent":
        previous["execution_commit"] = "8" * 40
    elif mutation == "nested":
        repair["source_snapshot_attestation"]["git_tree"] = "9" * 40
    else:
        repair["source_snapshot_attestation_active_path"] = "forged.json"
    with pytest.raises(mod.MissionFinalizationError, match="source snapshot"):
        mod._validate_repair03_source_verification(
            run,
            run_id=run.name,
            repair03=repair,
            previous_identity=previous,
            current_identity=current,
        )


def test_repair03_local_git_mode_rejects_snapshot_evidence(tmp_path):
    run = tmp_path / "synthetic-repair03"
    run.mkdir()
    repair = {"source_verification_mode": mod.REPAIR_03_GIT_SOURCE_MODE}
    mod._validate_repair03_source_verification(
        run,
        run_id=run.name,
        repair03=repair,
        previous_identity={},
        current_identity={},
    )
    repair["source_snapshot_attestation"] = {}
    with pytest.raises(mod.MissionFinalizationError, match="local-Git"):
        mod._validate_repair03_source_verification(
            run,
            run_id=run.name,
            repair03=repair,
            previous_identity={},
            current_identity={},
        )


def test_repair03_synthetic_prefix_overlay_reuses_strict_repair02_validator(
    tmp_path, monkeypatch
):
    run = tmp_path / "synthetic-repair03"
    pre = run / mod.REPAIR_03_PRE_ROOT
    pre.mkdir(parents=True)
    repair01 = {"repair_id": "repair-01"}
    repair02 = {"repair_id": "repair-02"}
    archived = {
        "run_id": run.name,
        "status": mod.REPAIR02_PENDING_STATUS,
        "registration_state": (
            "RE_FROZEN_AFTER_DATA_INTEGRITY_REPAIR02_BEFORE_RFQ_RESULT"
        ),
        "data_integrity_repairs": [repair01, repair02],
    }
    write_json(pre / "RUN_MANIFEST.json", archived)
    calls = []

    def strict_prefix(run_dir, manifest, rfq, **kwargs):
        calls.append((run_dir, manifest, rfq, kwargs))
        return {"prefix_only": True, "strict_repair02_prefix": True}

    monkeypatch.setattr(mod, "validate_rfq_double_quarantine", strict_prefix)
    result, observed_manifest, observed_path = mod._validate_repair03_prefix_overlay(
        run,
        run_id=run.name,
        repair01=repair01,
        repair02=repair02,
        rfq={"status": mod.RFQ_PARTIAL_STATUS},
    )
    assert result["strict_repair02_prefix"] is True
    assert observed_manifest == archived
    assert observed_path == pre / "RUN_MANIFEST.json"
    assert calls[0][3] == {
        "prefix_only": True,
        "active_boundary_root": mod.REPAIR_03_PRE_ROOT,
    }


def test_repair03_synthetic_prefix_overlay_rejects_non_parent_manifest(
    tmp_path, monkeypatch
):
    run = tmp_path / "synthetic-repair03"
    pre = run / mod.REPAIR_03_PRE_ROOT
    pre.mkdir(parents=True)
    write_json(
        pre / "RUN_MANIFEST.json",
        {
            "run_id": run.name,
            "status": mod.REPAIR03_PENDING_STATUS,
            "registration_state": "FORGED",
            "data_integrity_repairs": [],
        },
    )
    monkeypatch.setattr(
        mod,
        "validate_rfq_double_quarantine",
        lambda *_args, **_kwargs: pytest.fail("strict validator must not be reached"),
    )
    with pytest.raises(mod.MissionFinalizationError, match="repair-02 manifest"):
        mod._validate_repair03_prefix_overlay(
            run,
            run_id=run.name,
            repair01={"repair_id": "repair-01"},
            repair02={"repair_id": "repair-02"},
            rfq={},
        )


def test_repair03_dispatches_to_parser_validator(monkeypatch, tmp_path):
    expected = {"status": "repair03-validated"}
    monkeypatch.setattr(mod, "validate_rfq_parser_repair03", lambda *_: expected)
    manifest = {
        "data_integrity_repairs": [
            {"repair_id": "repair-01"},
            {"repair_id": "repair-02"},
            {"repair_id": "repair-03"},
        ]
    }
    assert mod.validate_rfq_partial_quarantine(tmp_path, manifest, {}) is expected


def test_repair03_manifest_status_reaches_frozen_source_validation(tmp_path, monkeypatch):
    class ReachedFrozenSource(Exception):
        pass

    manifest = {
        "status": mod.REPAIR03_PENDING_STATUS,
        "data_integrity_repairs": [
            {"repair_id": "repair-01"},
            {"repair_id": "repair-02"},
            {"repair_id": "repair-03"},
        ],
    }
    monkeypatch.setattr(mod, "validate_run_identity", lambda *_: (manifest, {}))
    monkeypatch.setattr(
        mod,
        "validate_frozen_query_source",
        lambda *_: (_ for _ in ()).throw(ReachedFrozenSource()),
    )
    with pytest.raises(ReachedFrozenSource):
        mod.finalize(tmp_path)


def repair03_success_outputs_fixture(tmp_path):
    """Build a complete consumer-shaped repair-03 success boundary."""
    run = tmp_path / "synthetic-repair03"
    for relative in ("DATA_INTEGRITY", "REPORT/tables", "logs/resources", "source", "queries"):
        (run / relative).mkdir(parents=True, exist_ok=True)

    registration_sha = "1" * 64
    parser_sha = "2" * 64
    authority_sha = "3" * 64
    audit_sha = "4" * 64
    source_payload = b"# repair-03 consumer\n"
    query_sha = hashlib.sha256(source_payload).hexdigest()
    quarantined_sha = "6" * 64
    full_sha = "7" * 64
    retained_sha = "8" * 64
    selection_sha = mod.EXPECTED_RFQ_REPAIR02_SELECTION_SHA256
    repair_chain_prefix = [
        {"repair_id": "repair-01"},
        {"repair_id": "repair-02"},
    ]
    failed_bindings_prefix = [
        {"repair_id": "repair-01"},
        {"repair_id": "repair-02"},
    ]
    release_receipts = [
        {"release_id": "release-a"},
        {"release_id": "release-b"},
    ]
    quarantine_details = [
        {"key": "raw_rfq/date=2026-07-13/rfq_23.ndjson.2", "reason": "malformed"},
        {"key": "raw_rfq/date=2026-07-13/rfq_24.ndjson", "reason": "malformed"},
    ]
    gap_plan = {
        "quarantined_object_count": 2,
        "contiguous_gap_count": 1,
        "observation_boundary_count": 2,
    }
    cycle_binding = {"path": "cache/cycle1.duckdb", "sha256": "9" * 64}
    prefix = {
        "repair_chain": repair_chain_prefix,
        "failed_attempt_bindings": failed_bindings_prefix,
        "selected_releases": release_receipts,
        "release_input_receipts": release_receipts,
        "overlap_keys": [],
        "retained_objects": [],
        "quarantine_details": quarantine_details,
        "full_set_sha256": full_sha,
        "retained_set_sha256": retained_sha,
        "quarantined_set_sha256": quarantined_sha,
        "selection_fingerprint_sha256": selection_sha,
        "quarantine_gap_plan": gap_plan,
        "cycle1_duckdb_binding": cycle_binding,
        "initial_identity": {
            "execution_commit": "a" * 40,
            "source_manifest_sha256": "a" * 64,
            "source_sha256s_sha256": "b" * 64,
            "query_set_sha256": "c" * 64,
        },
        "current_identity": {
            "execution_commit": "b" * 40,
            "source_manifest_sha256": "d" * 64,
            "source_sha256s_sha256": "e" * 64,
            "query_set_sha256": "f" * 64,
        },
        "repair02": {
            "declaration_sha256": "0" * 64,
            "receipt_sha256": "a" * 64,
        },
        "authorization02_sha256": "b" * 64,
    }
    chain03 = {
        "repair_id": "repair-03",
        "registration_path": mod.REPAIR_REGISTRATION_RECEIPT_03.as_posix(),
        "registration_sha256": registration_sha,
        "parser_contract_path": mod.REPAIR_03_PARSER_CONTRACT.as_posix(),
        "parser_contract_sha256": parser_sha,
        "authority_basis_path": mod.REPAIR_03_AUTHORITY_BASIS.as_posix(),
        "authority_basis_sha256": authority_sha,
        "inner_payload_audit_path": mod.ARCHIVED_RFQ_INNER_PAYLOAD_AUDIT_03.as_posix(),
        "inner_payload_audit_sha256": audit_sha,
    }
    repair_chain = [*repair_chain_prefix, chain03]
    failed = {
        "state_sha256": "c" * 64,
        "resource_sha256": "d" * 64,
        "scratch_sha256": "e" * 64,
        "input_sha256": "f" * 64,
        "preserved_scratch_sha256": "1" * 64,
        "preserved_scratch_bytes": 1,
    }
    failed03_binding = {
        "repair_id": "repair-03",
        "attempt_id": "RFQ_FULL_STAGE_REPAIR02_ATTEMPT_03",
        "failed_state_path": mod.FAILED_RFQ_STATE_03.as_posix(),
        "failed_state_sha256": failed["state_sha256"],
        "failed_resource_receipt_path": mod.FAILED_RFQ_RESOURCE_03.as_posix(),
        "failed_resource_receipt_sha256": failed["resource_sha256"],
        "failed_scratch_receipt_path": mod.FAILED_RFQ_SCRATCH_RECEIPT_03.as_posix(),
        "failed_scratch_receipt_sha256": failed["scratch_sha256"],
        "failed_input_identity_path": mod.FAILED_RFQ_INPUT_IDENTITY_03.as_posix(),
        "failed_input_identity_sha256": failed["input_sha256"],
        "input_fingerprint": selection_sha,
        "resource_label": "rfq_full_stage_repair02",
        "retry_requirement": "FRESH_SCRATCH_SAME_SELECTION_NEW_PARSER_CONTRACT_NO_RESUME",
    }
    failed_bindings = [*failed_bindings_prefix, failed03_binding]
    success_resource = {
        "label": "rfq_full_stage_repair03",
        "path": mod.ACTIVE_RFQ_REPAIR_RESOURCE_03.as_posix(),
    }
    parser_binding = {
        "path": mod.REPAIR_03_PARSER_CONTRACT.as_posix(),
        "sha256": parser_sha,
        "schema_version": "rfq-inner-payload-parser-contract-v1",
    }
    selection_fields = {
        "release_ids": ["release-a", "release-b"],
        "coverage_status": mod.RFQ_PARTIAL_STATUS,
        "full_object_coverage": False,
        "whole_object_quarantine": True,
        "line_salvage": False,
        "logical_manifest_bindings_total": mod.EXPECTED_RFQ_FULL_LOGICAL_BINDINGS,
        "unique_objects_total": mod.EXPECTED_RFQ_FULL_OBJECTS,
        "unique_bytes_total": mod.EXPECTED_RFQ_FULL_BYTES,
        "consumed_unique_objects": mod.EXPECTED_RFQ_RETAINED_OBJECTS,
        "consumed_logical_bindings": mod.EXPECTED_RFQ_RETAINED_LOGICAL_BINDINGS,
        "consumed_bytes": mod.EXPECTED_RFQ_RETAINED_BYTES,
        "consumed_object_set_sha256": retained_sha,
        "quarantined_unique_objects": mod.EXPECTED_RFQ_QUARANTINED_OBJECTS,
        "quarantined_logical_bindings": mod.EXPECTED_RFQ_QUARANTINED_LOGICAL_BINDINGS,
        "quarantined_bytes": mod.EXPECTED_RFQ_QUARANTINED_BYTES,
        "quarantined_object_set_sha256": quarantined_sha,
        "quarantine_reasons": [row["reason"] for row in quarantine_details],
        "quarantine_details": quarantine_details,
        "quarantine_gap_plan": gap_plan,
        "cycle1_duckdb_binding": cycle_binding,
        "deduplicated_overlapping_objects": 0,
        "manifest_object_set_sha256": full_sha,
        "selection_fingerprint_sha256": selection_sha,
    }
    stable_identity = {
        "run_id": run.name,
        **selection_fields,
        "failed_attempt_binding": None,
        "releases": release_receipts,
        "consumed_objects": [],
    }
    failed["input"] = {
        "schema": "rfq-full-input-identity-v2",
        **stable_identity,
        "repair_chain": repair_chain_prefix,
        "failed_attempt_bindings": failed_bindings_prefix,
        "expected_success_resource": {
            "label": "rfq_full_stage_repair02",
            "path": mod.ACTIVE_RFQ_REPAIR_RESOURCE_02.as_posix(),
        },
    }
    active_identity = {
        "schema": "rfq-full-input-identity-v3",
        **stable_identity,
        "repair_chain": repair_chain,
        "failed_attempt_bindings": failed_bindings,
        "expected_success_resource": success_resource,
        "inner_payload_parser_contract": parser_binding,
        "registered_rfq_query_sha256": query_sha,
    }
    write_json(run / mod.RFQ_INPUT_IDENTITY, active_identity)
    active_state = {
        "schema": "rfq-full-stage-state-v1",
        "run_id": run.name,
        "status": "COMPLETE_PARTIAL_OBJECT_COVERAGE_QUARANTINED",
        "phase": "COMPLETE",
        "resume": False,
        "input_fingerprint": selection_sha,
        "summary": mod.RFQ_SUMMARY.as_posix(),
        "repair_chain": repair_chain,
        "failed_attempt_bindings": failed_bindings,
        "quarantine_gap_plan": gap_plan,
        "expected_success_resource": success_resource,
        "started_at_utc": "2026-07-15T16:01:00Z",
        "completed_at_utc": "2026-07-15T16:30:00Z",
        "inner_payload_parser_contract": parser_binding,
        "registered_rfq_query_sha256": query_sha,
    }
    write_json(run / mod.ACTIVE_RFQ_STATE, active_state)

    gap = {
        "release_id": "release-a",
        "key": quarantine_details[0]["key"],
        "sha256": "2" * 64,
        "quarantined_keys_json": json.dumps(
            [row["key"] for row in quarantine_details], separators=(",", ":")
        ),
        "quarantined_object_count": 2,
        "quarantined_object_set_sha256": quarantined_sha,
        "previous_filename": "/cache/rfq_22.ndjson",
        "next_filename": "/cache/rfq_25.ndjson",
        "gap_start_ns": 1_000,
        "gap_end_ns": 2_000,
        "gap_start_us": 1,
        "gap_end_us": 2,
        "boundary_reason": "WHOLE_OBJECT_QUARANTINE_MALFORMED_NDJSON",
    }
    coverage = {
        "rfq_object_coverage": mod.RFQ_PARTIAL_STATUS,
        "quarantine_gap_ranges": [gap],
        "quarantine_gap_set_sha256": mod.canonical_json_sha256([gap]),
        "quarantined_hours_are_not_observed_zero": True,
        "capture_completeness_is_not_lifecycle_join_completeness": True,
        "partial_object_coverage_hours": 1,
        "quarantine_gap_count": 1,
        "quarantined_object_count": 2,
        "quarantine_observation_boundary_count": 2,
    }
    with (run / mod.RFQ_QUARANTINE_GAPS).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(gap))
        writer.writeheader()
        writer.writerow(gap)
    (run / mod.RFQ_HOUR_COVERAGE).write_text(
        "object_coverage_status,zero_interpretation\n"
        f"{mod.RFQ_PARTIAL_STATUS},NOT_AN_OBSERVED_ZERO_QUARANTINE_OVERLAP\n",
        encoding="utf-8",
    )
    source = run / "source/rfq_full_stage.py"
    frozen = run / "queries/rfq_full_stage.py"
    source.write_bytes(source_payload)
    frozen.write_bytes(source_payload)
    write_json(
        run / mod.ACTIVE_RFQ_REPAIR_RESOURCE_03,
        {
            "schema_version": "w09-stage-resource-v1",
            "label": "rfq_full_stage_repair03",
            "started_at_utc": "2026-07-15T16:00:00Z",
            "completed_at_utc": "2026-07-15T16:31:00Z",
            "wall_seconds": 1_860.0,
            "return_code": 0,
            "command": [
                "/opt/w09/venv/bin/python",
                str(source.resolve()),
                "--run-dir",
                str(run.resolve()),
                "--cache-root",
                "/srv/w09-research/cache",
                "--memory-limit",
                "40GB",
                "--max-temp-size",
                "120GB",
                "--threads",
                "8",
                "--min-free-gib",
                "120",
                "--clob-max-per-root",
                "50",
            ],
        },
    )
    rfq_input = {
        **selection_fields,
        "failed_attempt_binding": None,
        "repair_chain": repair_chain,
        "failed_attempt_bindings": failed_bindings,
        "expected_success_resource": success_resource,
        "inner_payload_parser_contract": parser_binding,
        "registered_rfq_query_sha256": query_sha,
        "outer_parser": "STRICT_NDJSON_IGNORE_ERRORS_FALSE; malformed outer rows abort",
        "overlap_keys": [],
    }
    rfq = {
        "schema": "sports-autoresearch-rfq-full-stage-v1",
        "run_id": run.name,
        "generated_at_utc": "2026-07-15T16:20:00Z",
        "status": mod.RFQ_PARTIAL_STATUS,
        "analysis_scope": mod.RFQ_ANALYSIS_SCOPE,
        "input": rfq_input,
        "coverage": coverage,
        "counts": {"observation_boundary_timestamps": 2},
    }
    write_json(run / mod.RFQ_SUMMARY, rfq)
    validation_args = dict(
        run_dir=run,
        manifest={"run_id": run.name},
        rfq=rfq,
        repair03={
            "registered_rfq_query_sha256": query_sha,
            "inherited_declaration_sha256": "0" * 64,
            "inherited_receipt_sha256": "a" * 64,
        },
        repair03_receipt_sha=registration_sha,
        parser_contract_sha=parser_sha,
        authority_sha=authority_sha,
        audit_sha=audit_sha,
        prefix=prefix,
        failed=failed,
        current_identity={
            "execution_commit": "c" * 40,
            "source_manifest_sha256": "1" * 64,
            "source_sha256s_sha256": "2" * 64,
            "query_set_sha256": "3" * 64,
        },
    )

    return {
        "run": run,
        "validation_args": validation_args,
        "repair_chain": repair_chain,
        "resource_path": run / mod.ACTIVE_RFQ_REPAIR_RESOURCE_03,
        "state_path": run / mod.ACTIVE_RFQ_STATE,
        "summary_path": run / mod.RFQ_SUMMARY,
        "source_path": source,
        "frozen_source_path": frozen,
    }


def test_repair03_success_outputs_complete_positive_path(tmp_path):
    """Exercise the entire repair-03 success validator with exact execution evidence."""
    fixture = repair03_success_outputs_fixture(tmp_path)
    result = mod._validate_repair03_success_outputs(**fixture["validation_args"])

    assert result["status"] == mod.RFQ_PARTIAL_STATUS
    assert result["repair_chain"] == fixture["repair_chain"]
    assert set(result["repair_chain"][2]) == {
        "repair_id",
        "registration_path",
        "registration_sha256",
        "parser_contract_path",
        "parser_contract_sha256",
        "authority_basis_path",
        "authority_basis_sha256",
        "inner_payload_audit_path",
        "inner_payload_audit_sha256",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "command_source_path",
        "command_run_dir_same_basename",
        "command_threads",
        "command_keep_scratch",
        "resource_starts_after_summary",
        "state_starts_before_resource",
        "resource_completes_before_state",
        "summary_generated_after_state",
        "state_completed_before_summary",
        "state_summary_path",
        "summary_file_binding",
        "executed_source_not_registered",
    ],
)
def test_repair03_success_outputs_reject_execution_evidence_mutations(
    tmp_path, mutation
):
    fixture = repair03_success_outputs_fixture(tmp_path)
    args = fixture["validation_args"]
    resource = json.loads(fixture["resource_path"].read_text(encoding="utf-8"))
    state = json.loads(fixture["state_path"].read_text(encoding="utf-8"))

    if mutation == "command_source_path":
        resource["command"][1] = str(
            (fixture["run"].parent / "forged" / "source/rfq_full_stage.py").resolve()
        )
    elif mutation == "command_run_dir_same_basename":
        resource["command"][3] = str(
            (fixture["run"].parent / "forged" / fixture["run"].name).resolve()
        )
    elif mutation == "command_threads":
        resource["command"][resource["command"].index("--threads") + 1] = "16"
    elif mutation == "command_keep_scratch":
        resource["command"].append("--keep-scratch")
    elif mutation == "resource_starts_after_summary":
        resource["started_at_utc"] = "2026-07-15T16:21:00Z"
    elif mutation == "state_starts_before_resource":
        state["started_at_utc"] = "2026-07-15T15:59:00Z"
    elif mutation == "resource_completes_before_state":
        resource["completed_at_utc"] = "2026-07-15T16:29:00Z"
    elif mutation == "summary_generated_after_state":
        args["rfq"]["generated_at_utc"] = "2026-07-15T16:32:00Z"
    elif mutation == "state_completed_before_summary":
        state["completed_at_utc"] = "2026-07-15T16:19:00Z"
    elif mutation == "state_summary_path":
        state["summary"] = "REPORT/tables/forged.json"
    elif mutation == "summary_file_binding":
        forged_summary = dict(args["rfq"])
        forged_summary["status"] = "FORGED"
        write_json(fixture["summary_path"], forged_summary)
    else:
        forged = b"# unregistered repair-03 consumer\n"
        fixture["source_path"].write_bytes(forged)
        fixture["frozen_source_path"].write_bytes(forged)

    write_json(fixture["resource_path"], resource)
    write_json(fixture["state_path"], state)
    if mutation != "summary_file_binding":
        write_json(fixture["summary_path"], args["rfq"])
    with pytest.raises(mod.MissionFinalizationError):
        mod._validate_repair03_success_outputs(**args)


def repair04_success_outputs_fixture(tmp_path):
    """Upgrade the exact repair-03 fixture into a resource-only v4 success."""
    fixture = repair03_success_outputs_fixture(tmp_path)
    args03 = fixture["validation_args"]
    run = fixture["run"]
    failed04_input = json.loads(
        (run / mod.RFQ_INPUT_IDENTITY).read_text(encoding="utf-8")
    )
    repair03_context = {
        "prefix": args03["prefix"],
        "failed": args03["failed"],
        "repair03": args03["repair03"],
        "repair03_receipt_sha256": args03["repair03_receipt_sha"],
        "parser_contract_sha256": args03["parser_contract_sha"],
        "authority_basis_sha256": args03["authority_sha"],
        "inner_payload_audit_sha256": args03["audit_sha"],
        "current_identity": args03["current_identity"],
        "registered_rfq_query_sha256": args03["repair03"][
            "registered_rfq_query_sha256"
        ],
    }
    failed04 = {
        "state_sha256": "5" * 64,
        "resource_sha256": "6" * 64,
        "scratch_sha256": "7" * 64,
        "input_sha256": "8" * 64,
        "preserved_scratch_sha256": "9" * 64,
        "preserved_scratch_bytes": 10,
        "input": failed04_input,
    }
    repair04_receipt_sha = "a" * 64
    contract_sha = "b" * 64
    authority_sha = "c" * 64
    repair_chain, failed_bindings, parser_binding, resource_binding = (
        mod._repair04_runtime_bindings(
            repair03_context=repair03_context,
            repair04_receipt_sha=repair04_receipt_sha,
            resource_contract_sha=contract_sha,
            authority_sha=authority_sha,
            failed=failed04,
        )
    )
    source_payload = b"# repair-04 resource contract consumer\n"
    query_sha = hashlib.sha256(source_payload).hexdigest()
    expected_resource = {
        "label": "rfq_full_stage_repair04",
        "path": mod.ACTIVE_RFQ_REPAIR_RESOURCE_04.as_posix(),
    }

    active_identity = copy.deepcopy(failed04_input)
    active_identity.update(
        {
            "schema": "rfq-full-input-identity-v4",
            "repair_chain": repair_chain,
            "failed_attempt_bindings": failed_bindings,
            "expected_success_resource": expected_resource,
            "registered_rfq_query_sha256": query_sha,
            "rfq_resource_contract": resource_binding,
        }
    )
    write_json(run / mod.RFQ_INPUT_IDENTITY, active_identity)

    active_state = json.loads(
        (run / mod.ACTIVE_RFQ_STATE).read_text(encoding="utf-8")
    )
    active_state.update(
        {
            "repair_chain": repair_chain,
            "failed_attempt_bindings": failed_bindings,
            "expected_success_resource": expected_resource,
            "registered_rfq_query_sha256": query_sha,
            "rfq_resource_contract": resource_binding,
            "started_at_utc": "2026-07-15T17:31:00Z",
            "completed_at_utc": "2026-07-15T18:30:00Z",
        }
    )
    write_json(run / mod.ACTIVE_RFQ_STATE, active_state)

    fixture["frozen_source_path"].write_bytes(source_payload)
    resource_path = run / mod.ACTIVE_RFQ_REPAIR_RESOURCE_04
    write_json(
        resource_path,
        {
            "schema_version": "w09-stage-resource-v1",
            "label": "rfq_full_stage_repair04",
            "started_at_utc": "2026-07-15T17:30:00Z",
            "completed_at_utc": "2026-07-15T18:31:00Z",
            "wall_seconds": 3_660.0,
            "return_code": 0,
            "s3_bytes_read_by_analysis": 0,
            "command": [
                "/opt/w09/venv/bin/python",
                str(fixture["frozen_source_path"].resolve()),
                "--run-dir",
                str(run.resolve()),
                "--cache-root",
                "/srv/w09-research/cache",
                "--memory-limit",
                "46GB",
                "--max-temp-size",
                "70GB",
                "--threads",
                "4",
                "--min-free-gib",
                "100",
                "--clob-max-per-root",
                "50",
            ],
        },
    )

    rfq = copy.deepcopy(args03["rfq"])
    rfq["generated_at_utc"] = "2026-07-15T18:20:00Z"
    rfq["input"].update(
        {
            "repair_chain": repair_chain,
            "failed_attempt_bindings": failed_bindings,
            "expected_success_resource": expected_resource,
            "inner_payload_parser_contract": parser_binding,
            "registered_rfq_query_sha256": query_sha,
            "rfq_resource_contract": resource_binding,
        }
    )
    write_json(run / mod.RFQ_SUMMARY, rfq)
    current_identity = {
        "execution_commit": "d" * 40,
        "source_manifest_sha256": "d" * 64,
        "source_sha256s_sha256": "e" * 64,
        "query_set_sha256": "f" * 64,
    }
    validation_args = {
        "run_dir": run,
        "manifest": {"run_id": run.name},
        "rfq": rfq,
        "repair04": {"registered_rfq_query_sha256": query_sha},
        "repair04_receipt_sha": repair04_receipt_sha,
        "resource_contract_sha": contract_sha,
        "authority_sha": authority_sha,
        "repair03_context": repair03_context,
        "failed": failed04,
        "current_identity": current_identity,
    }
    return {
        "run": run,
        "validation_args": validation_args,
        "resource_path": resource_path,
        "state_path": run / mod.ACTIVE_RFQ_STATE,
        "identity_path": run / mod.RFQ_INPUT_IDENTITY,
        "summary_path": run / mod.RFQ_SUMMARY,
        "source_path": fixture["source_path"],
        "frozen_source_path": fixture["frozen_source_path"],
        "repair_chain": repair_chain,
        "failed_bindings": failed_bindings,
        "resource_binding": resource_binding,
    }


def test_repair04_success_outputs_complete_positive_path(tmp_path):
    fixture = repair04_success_outputs_fixture(tmp_path)
    assert fixture["source_path"].read_bytes() != (
        fixture["frozen_source_path"].read_bytes()
    )
    result = mod._validate_repair04_success_outputs(
        **fixture["validation_args"]
    )

    assert result["status"] == mod.RFQ_PARTIAL_STATUS
    assert result["repair_chain"] == fixture["repair_chain"]
    assert result["failed_attempt_bindings"] == fixture["failed_bindings"]
    assert result["resource_contract_path"] == (
        mod.REPAIR_04_RESOURCE_CONTRACT.as_posix()
    )
    resource = json.loads(fixture["resource_path"].read_text(encoding="utf-8"))
    assert resource["command"][1] == str(
        (fixture["run"] / mod.REPAIR_04_EXECUTION_QUERY).resolve()
    )
    assert set(result["repair_chain"][-1]) == {
        "repair_id",
        "registration_path",
        "registration_sha256",
        "resource_contract_path",
        "resource_contract_sha256",
        "authority_basis_path",
        "authority_basis_sha256",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "identity_schema_v3",
        "identity_parser_changed",
        "identity_selection_changed",
        "identity_resource_changed",
        "state_resource_changed",
        "summary_resource_changed",
        "command_memory",
        "command_threads",
        "command_keep_scratch",
        "command_legacy_source_path",
        "resource_label",
        "resource_starts_after_summary",
        "state_starts_before_resource",
        "resource_completes_before_state",
        "executed_source_not_registered",
    ],
)
def test_repair04_success_outputs_reject_mutations(tmp_path, mutation):
    fixture = repair04_success_outputs_fixture(tmp_path)
    args = fixture["validation_args"]
    identity = json.loads(fixture["identity_path"].read_text(encoding="utf-8"))
    state = json.loads(fixture["state_path"].read_text(encoding="utf-8"))
    resource = json.loads(fixture["resource_path"].read_text(encoding="utf-8"))

    if mutation == "identity_schema_v3":
        identity["schema"] = "rfq-full-input-identity-v3"
    elif mutation == "identity_parser_changed":
        identity["inner_payload_parser_contract"]["sha256"] = "0" * 64
    elif mutation == "identity_selection_changed":
        identity["consumed_bytes"] += 1
    elif mutation == "identity_resource_changed":
        identity["rfq_resource_contract"]["current_runtime"]["threads"] = 8
    elif mutation == "state_resource_changed":
        state["rfq_resource_contract"]["current_runtime"]["threads"] = 8
    elif mutation == "summary_resource_changed":
        args["rfq"]["input"]["rfq_resource_contract"]["current_runtime"][
            "threads"
        ] = 8
    elif mutation == "command_memory":
        index = resource["command"].index("--memory-limit") + 1
        resource["command"][index] = "40GB"
    elif mutation == "command_threads":
        index = resource["command"].index("--threads") + 1
        resource["command"][index] = "8"
    elif mutation == "command_keep_scratch":
        resource["command"].append("--keep-scratch")
    elif mutation == "command_legacy_source_path":
        resource["command"][1] = str(fixture["source_path"].resolve())
    elif mutation == "resource_label":
        resource["label"] = "rfq_full_stage_repair03"
    elif mutation == "resource_starts_after_summary":
        resource["started_at_utc"] = "2026-07-15T18:21:00Z"
    elif mutation == "state_starts_before_resource":
        state["started_at_utc"] = "2026-07-15T17:29:00Z"
    elif mutation == "resource_completes_before_state":
        resource["completed_at_utc"] = "2026-07-15T18:29:00Z"
    else:
        forged = b"# unregistered repair-04 consumer\n"
        fixture["frozen_source_path"].write_bytes(forged)

    write_json(fixture["identity_path"], identity)
    write_json(fixture["state_path"], state)
    write_json(fixture["resource_path"], resource)
    write_json(fixture["summary_path"], args["rfq"])
    with pytest.raises(mod.MissionFinalizationError):
        mod._validate_repair04_success_outputs(**args)


def test_repair04_runtime_bindings_are_resource_only(tmp_path):
    fixture = repair04_success_outputs_fixture(tmp_path)
    chain = fixture["repair_chain"][-1]
    failed = fixture["failed_bindings"][-1]
    resource = fixture["resource_binding"]

    assert chain["repair_id"] == "repair-04"
    assert set(failed) == {
        "repair_id",
        "attempt_id",
        "failed_state_path",
        "failed_state_sha256",
        "failed_resource_receipt_path",
        "failed_resource_receipt_sha256",
        "failed_scratch_receipt_path",
        "failed_scratch_receipt_sha256",
        "failed_input_identity_path",
        "failed_input_identity_sha256",
        "input_fingerprint",
        "resource_label",
        "retry_requirement",
    }
    assert resource["current_runtime"] == {
        "memory_limit": "46GB",
        "max_temp_size": "70GB",
        "threads": 4,
        "min_free_gib": 100.0,
        "clob_max_per_root": 50,
        "resume": False,
        "keep_scratch": False,
    }


def repair04_resource_contract_fixture(tmp_path):
    run = tmp_path / "synthetic-repair04-contract"
    (run / mod.REPAIR_04_ROOT).mkdir(parents=True)
    evidence = {
        "failed_state_path": mod.FAILED_RFQ_STATE_04.as_posix(),
        "failed_state_sha256": mod.EXPECTED_RFQ_FAILED_STATE_04_SHA256,
        "failed_resource_receipt_path": mod.FAILED_RFQ_RESOURCE_04.as_posix(),
        "failed_resource_receipt_sha256": (
            mod.EXPECTED_RFQ_FAILED_RESOURCE_04_SHA256
        ),
        "failed_input_identity_path": mod.FAILED_RFQ_INPUT_IDENTITY_04.as_posix(),
        "failed_input_identity_sha256": mod.EXPECTED_RFQ_FAILED_INPUT_04_SHA256,
        "failed_scratch_receipt_path": (
            mod.FAILED_RFQ_SCRATCH_RECEIPT_04.as_posix()
        ),
        "failed_scratch_receipt_sha256": (
            mod.EXPECTED_RFQ_FAILED_SCRATCH_RECEIPT_04_SHA256
        ),
    }
    recorded_at = "2026-07-15T18:45:00Z"
    contract = repair04_registrar.make_resource_contract(
        run.name, recorded_at, evidence
    )
    contract_path = run / mod.REPAIR_04_RESOURCE_CONTRACT
    write_json(contract_path, contract)
    contract_sha = mod.sha256(contract_path)
    authority = repair04_registrar.make_authority_basis(
        run.name,
        recorded_at,
        mod.REPAIR_04_RESOURCE_CONTRACT.as_posix(),
        contract_sha,
    )
    authority_path = run / mod.REPAIR_04_AUTHORITY_BASIS
    write_json(authority_path, authority)
    repair = {
        "resource_contract_path": mod.REPAIR_04_RESOURCE_CONTRACT.as_posix(),
        "resource_contract_sha256": contract_sha,
        "authority_basis_path": mod.REPAIR_04_AUTHORITY_BASIS.as_posix(),
        "authority_basis_sha256": mod.sha256(authority_path),
    }
    return {
        "run": run,
        "recorded_at": recorded_at,
        "contract": contract,
        "contract_path": contract_path,
        "authority": authority,
        "authority_path": authority_path,
        "repair": repair,
    }


def test_repair04_resource_contract_and_authority_are_exact(tmp_path):
    fixture = repair04_resource_contract_fixture(tmp_path)
    contract, contract_sha, authority, authority_sha = (
        mod._validate_repair04_resource_and_authority(
            fixture["run"],
            run_id=fixture["run"].name,
            repair04=fixture["repair"],
        )
    )

    assert contract == fixture["contract"]
    assert authority == fixture["authority"]
    assert contract["expected_command"][1] == (
        f"/srv/w09-research/runs/{fixture['run'].name}/queries/rfq_full_stage.py"
    )
    assert contract_sha == fixture["repair"]["resource_contract_sha256"]
    assert authority_sha == fixture["repair"]["authority_basis_sha256"]


@pytest.mark.parametrize(
    "mutation",
    [
        "previous_runtime",
        "current_memory",
        "current_threads",
        "expected_command",
        "failed_error",
        "memory_fraction",
        "disk_projection",
        "success_resource",
        "authority_spend",
        "authority_timestamp",
    ],
)
def test_repair04_resource_contract_mutations_fail_closed(tmp_path, mutation):
    fixture = repair04_resource_contract_fixture(tmp_path)
    contract = copy.deepcopy(fixture["contract"])
    authority = copy.deepcopy(fixture["authority"])

    if mutation == "previous_runtime":
        contract["previous_runtime"]["threads"] = 4
    elif mutation == "current_memory":
        contract["current_runtime"]["memory_limit"] = "48GB"
    elif mutation == "current_threads":
        contract["current_runtime"]["threads"] = 8
    elif mutation == "expected_command":
        index = contract["expected_command"].index("--threads") + 1
        contract["expected_command"][index] = "8"
    elif mutation == "failed_error":
        contract["failed_attempt"]["error"] = "forged OOM"
    elif mutation == "memory_fraction":
        contract["memory_safety"]["memory_limit_fraction_of_memtotal"] = 0.8
    elif mutation == "disk_projection":
        contract["disk_safety"][
            "projected_minimum_disk_free_bytes_using_previous_delta"
        ] += 1
    elif mutation == "success_resource":
        contract["expected_success_resource"]["label"] = (
            "rfq_full_stage_repair03"
        )
    elif mutation == "authority_spend":
        authority["new_instance_spend_authorized"] = True
    else:
        authority["recorded_at_utc"] = "2026-07-15T18:46:00Z"

    write_json(fixture["contract_path"], contract)
    fixture["repair"]["resource_contract_sha256"] = mod.sha256(
        fixture["contract_path"]
    )
    if not mutation.startswith("authority_"):
        authority = repair04_registrar.make_authority_basis(
            fixture["run"].name,
            fixture["recorded_at"],
            mod.REPAIR_04_RESOURCE_CONTRACT.as_posix(),
            fixture["repair"]["resource_contract_sha256"],
        )
    else:
        authority["resource_contract_sha256"] = fixture["repair"][
            "resource_contract_sha256"
        ]
    write_json(fixture["authority_path"], authority)
    fixture["repair"]["authority_basis_sha256"] = mod.sha256(
        fixture["authority_path"]
    )

    with pytest.raises(mod.MissionFinalizationError):
        mod._validate_repair04_resource_and_authority(
            fixture["run"],
            run_id=fixture["run"].name,
            repair04=fixture["repair"],
        )


def test_repair04_dispatches_to_resource_validator(monkeypatch, tmp_path):
    expected = {"status": "repair04-validated"}
    monkeypatch.setattr(
        mod, "validate_rfq_resource_repair04", lambda *_: expected
    )
    manifest = {
        "data_integrity_repairs": [
            {"repair_id": "repair-01"},
            {"repair_id": "repair-02"},
            {"repair_id": "repair-03"},
            {"repair_id": "repair-04"},
        ]
    }

    assert mod.validate_rfq_partial_quarantine(tmp_path, manifest, {}) is expected


def test_repair02_claim_boundary_is_two_adjacent_objects_one_gap():
    assert "exactly two registered adjacent whole-object quarantines" in (
        mod.RFQ_PARTIAL_BOUNDARY
    )
    assert "one contiguous missing interval" in mod.RFQ_PARTIAL_BOUNDARY
    assert "two missing intervals" not in mod.RFQ_PARTIAL_BOUNDARY


@pytest.mark.parametrize(
    "value",
    [
        "2026-07-15T14:24:04Z",
        "2026-07-15T14:24:04+00:00",
        "2026-07-15T14:24:04.4Z",
        "2026-07-15T14:24:04.437126+00:00",
        "2026-07-15T14:24:04.437126672+00:00",
    ],
)
def test_require_utc_timestamp_accepts_strict_rfc3339_and_preserves_text(value):
    assert mod._require_utc_timestamp(value, "attempt-02 scratch mtime") == value


@pytest.mark.parametrize(
    "value",
    [
        "2026-07-15T14:24:04.437126672-04:00",
        "2026-07-15T14:24:04.437126672-00:00",
        "2026-02-30T14:24:04Z",
        "2026-07-15T14:24:04.1234567890Z",
        "2026-07-15T14:24:04.Z",
        "2026-07-15 14:24:04Z",
        "2026-07-15T14:24:04Zgarbage",
    ],
)
def test_require_utc_timestamp_rejects_non_utc_or_malformed_mutations(value):
    with pytest.raises(mod.MissionFinalizationError):
        mod._require_utc_timestamp(value, "attempt-02 scratch mtime")
