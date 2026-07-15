import importlib.util
import hashlib
import json
from pathlib import Path

import pytest


path = Path(__file__).with_name("finalize_mission.py")
spec = importlib.util.spec_from_file_location("finalize_mission", path)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mod)


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
