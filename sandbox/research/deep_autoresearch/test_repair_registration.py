import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).with_name("repair_registration.py")
SPEC = importlib.util.spec_from_file_location("repair_registration", MODULE_PATH)
repair = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(repair)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def command(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        list(args), cwd=cwd, text=True, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def git_commit(repo: Path, message: str) -> str:
    command(repo, "git", "add", "sandbox/research/deep_autoresearch")
    command(repo, "git", "commit", "-m", message)
    return command(repo, "git", "rev-parse", "HEAD")


def make_fixture(tmp_path: Path) -> dict:
    repo = tmp_path / "repo"
    source = repo / "sandbox/research/deep_autoresearch"
    source.mkdir(parents=True)
    command(repo, "git", "init")
    command(repo, "git", "config", "user.name", "Repair Test")
    command(repo, "git", "config", "user.email", "repair@example.invalid")
    (source / "analysis.py").write_text("VERSION = 1\n", encoding="utf-8")
    (source / "helper.py").write_text("HELPER = 'old'\n", encoding="utf-8")
    (source / "driver.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    old_commit = git_commit(repo, "old frozen execution")

    run = tmp_path / "20260715T112538Z__c21a79a8cff__deep01"
    (run / "queries").mkdir(parents=True)
    (run / "REPORT/tables").mkdir(parents=True)
    (run / "logs/resources").mkdir(parents=True)
    (run / "DATA_INTEGRITY/manifests").mkdir(parents=True)
    (run / "DATA_INTEGRITY/verified").mkdir(parents=True)
    (run / "queries/analysis.py").write_bytes((source / "analysis.py").read_bytes())

    source_rows = []
    for path in sorted(source.iterdir()):
        if path.is_file() and path.suffix in (".py", ".sh"):
            source_rows.append({
                "path": path.relative_to(repo).as_posix(),
                "sha256": digest(path),
                "bytes": path.stat().st_size,
            })
    write_json(run / "SOURCE_MANIFEST.json", source_rows)
    (run / "SOURCE_SHA256SUMS.txt").write_text(
        "".join(f"{row['sha256']}  {Path(row['path']).name}\n" for row in source_rows),
        encoding="utf-8",
    )
    (run / "QUERY_SHA256SUMS.txt").write_text(
        f"{digest(run / 'queries/analysis.py')}  queries/analysis.py\n",
        encoding="utf-8",
    )
    write_json(run / "FEATURE_DICTIONARY.json", {"features": []})
    (run / "METHODS.md").write_text("# frozen methods\n", encoding="utf-8")

    release_id = "2026-07-13__seal-test__pub-test"
    key = "raw_rfq/date=2026-07-14/rfq_00.ndjson"
    object_sha = "2" * 64
    version_id = "fixed-version-id"
    object_size = 268434075
    evidence_tier = "SEALED_DEGRADED_EVIDENCE"
    tl1_status = "TL1"
    publication_sha = "6" * 64
    seal_sha = "7" * 64
    version_bindings_sha = "8" * 64
    data_manifest_path = run / "DATA_INTEGRITY/manifests" / f"{release_id}.json"
    write_json(data_manifest_path, {
        "schema_version": "research-release-manifest-v2",
        "release_id": release_id,
        "date": "2026-07-13",
        "evidence_tier": evidence_tier,
        "tl1_status": tl1_status,
        "publication_state_sha256": publication_sha,
        "seal": {"status": "SEALED", "sha256": seal_sha},
        "version_binding": {
            "mode": "VERSION_BOUND", "bindings_sha256": version_bindings_sha,
        },
        "objects": [{
            "key": key, "sha256": object_sha, "size": object_size,
            "version_id": version_id,
        }],
    })
    manifest_sha = digest(data_manifest_path)
    write_json(run / "DATA_INTEGRITY/verified/2026-07-13.json", {
        "release_id": release_id,
        "date": "2026-07-13",
        "evidence_tier": evidence_tier,
        "tl1_status": tl1_status,
        "version_binding_mode": "VERSION_BOUND",
        "publication_state_sha256": publication_sha,
        "seal_sha256": seal_sha,
        "objects_verified": 1,
        "bytes_verified": object_size,
        "corrections_total": 0,
        "rfq_included": True,
        "rfq_status": "VERIFIED_SEALED_RAW",
    })
    union_objects = [{
        "bound_release_ids": [release_id],
        "key": key,
        "sha256": object_sha,
        "size": object_size,
    }]
    input_fingerprint = repair.rfq_object_fingerprint(union_objects)
    write_json(run / repair.FAILED_INPUT_IDENTITY, {
        "deduplicated_overlapping_objects": 0,
        "logical_manifest_bindings": 1,
        "objects": union_objects,
        "path_size_sha_fingerprint": input_fingerprint,
        "release_ids": [release_id],
        "releases": [{
            "manifest_sha256": manifest_sha,
            "release_id": release_id,
            "rfq_bytes": object_size,
            "rfq_objects": 1,
            # Captured from W09 cache .VERIFIED.json; deliberately distinct
            # from the date-scoped run receipt's SHA.
            "verified_marker_sha256": "5" * 64,
        }],
        "schema": "rfq-full-input-identity-v1",
        "unique_objects": 1,
    })

    trial_before = b"".join(
        (
            json.dumps({
                "trial_id": trial_id, "family": "rfq", "status": "REGISTERED",
                "result_opened": False,
            }, sort_keys=True) + "\n"
        ).encode("utf-8")
        for trial_id in repair.RFQ_TRIAL_IDS
    )
    (run / "TRIAL_REGISTRY.jsonl").write_bytes(trial_before)
    core_before = b'{"stage":"core","fixed":true}\n'
    (run / "REPORT/CYCLE1_CORE_SUMMARY.json").write_bytes(core_before)
    write_json(run / "REPORT/tables/CORE_HYPOTHESIS_TESTS.json", {"fixed": "core"})
    write_json(
        run / "REPORT/tables/L2_HYPOTHESIS_STAGE_SUMMARY.json", {"fixed": "l2"}
    )
    write_json(
        run / "REPORT/tables/RFQ_TRIGGER_REPRODUCTION.json", {"fixed": "trigger"}
    )
    write_json(run / repair.CORE_RESOURCE, {
        "schema_version": "w09-stage-resource-v1",
        "label": "cycle1_core",
        "return_code": 0,
    })
    write_json(run / repair.CYCLE1_DUCKDB_BINDING, {
        "bytes": 2492739584,
        "core_result_disposition": (
            "CORE_DERIVED_DATABASE_PRESERVED_NOT_RECOMPUTED"
        ),
        "core_stage_resource_path": repair.CORE_RESOURCE.as_posix(),
        "core_stage_resource_sha256": digest(run / repair.CORE_RESOURCE),
        "core_summary_path": repair.CORE_SUMMARY.as_posix(),
        "core_summary_sha256": digest(run / repair.CORE_SUMMARY),
        "created_before_rfq_repair_registration": True,
        "duckdb_version": "1.4.5",
        "mtime_utc": "2026-07-15T12:58:24+00:00",
        "path": f"/srv/w09-research/runs/{run.name}/cache/cycle1.duckdb",
        "run_id": run.name,
        "schema_version": "cycle1-derived-duckdb-binding-v1",
        "sha256": "9" * 64,
    })
    state = {
        "schema": "rfq-full-stage-state-v1",
        "status": "FAILED_RESUMABLE",
        "resume": False,
        "input_fingerprint": input_fingerprint,
        "scratch": f"/srv/w09-research/runs/{run.name}/cache/rfq_full_scratch.duckdb",
        "error": f"Malformed JSON in {key}",
    }
    write_json(run / repair.FAILURE_STATE, state)
    write_json(run / repair.FAILURE_RESOURCE, {
        "schema_version": "w09-stage-resource-v1",
        "label": "rfq_full_stage",
        "return_code": 1,
        "command": ["python", "rfq_full_stage.py", "--run-dir", str(run)],
    })
    write_json(run / repair.FAILURE_SCRATCH_RECEIPT, {
        "schema_version": "rfq-failed-scratch-receipt-v1",
        "run_id": run.name,
        "original_scratch_path": state["scratch"],
        "preserved_scratch_path": state["scratch"] + ".attempt-01-failed",
        "sha256": "3" * 64,
        "bytes": 25_000_000_000,
        "mtime_utc": "2026-07-15T13:20:33Z",
        "input_fingerprint": state["input_fingerprint"],
        "disposition": "PRESERVED_RENAMED_NO_RESUME",
        "resume_allowed": False,
    })

    manifest = {
        "run_id": run.name,
        "mode": "EXPLORATORY_AUTORESEARCH",
        "status": repair.EXPECTED_STATUS,
        "analysis_started": True,
        "selected_releases": [{
            "release_id": release_id,
            "date": "2026-07-13",
            "evidence_tier": evidence_tier,
            "tl1_status": tl1_status,
            "include": "EXPLORATORY_ONLY",
            "manifest_sha256": manifest_sha,
            "publication_state_sha256": publication_sha,
            "seal_sha256": seal_sha,
            "version_binding_mode": "VERSION_BOUND",
            "version_bindings_sha256": version_bindings_sha,
            "object_count": 1,
            "byte_count": object_size,
            "corrections_total": 0,
        }],
        "repository": {
            "bootstrap_commit": old_commit,
            "execution_commit": old_commit,
            "source_tree_dirty_at_freeze": False,
            "source_manifest_path": "SOURCE_MANIFEST.json",
            "source_manifest_sha256": digest(run / "SOURCE_MANIFEST.json"),
            "query_set_sha256": digest(run / "QUERY_SHA256SUMS.txt"),
            "query_files": ["queries/analysis.py"],
            "feature_definition_sha256": digest(run / "FEATURE_DICTIONARY.json"),
            "method_definition_sha256": digest(run / "METHODS.md"),
        },
    }
    write_json(run / "RUN_MANIFEST.json", manifest)

    receipt_path = run / "DATA_INTEGRITY/RFQ_MALFORMED_OBJECT_RECEIPT.json"
    write_json(receipt_path, {
        "schema_version": repair.RECEIPT_SCHEMA,
        "run_id": run.name,
        "release_id": release_id,
        "key": key,
        "expected_size": object_size,
        "observed_size": object_size,
        "expected_sha256": object_sha,
        "observed_sha256": object_sha,
        "invalid_line_count": 1,
        "invalid_lines": [{
            "line_number": 848,
            "line_sha256": "4" * 64,
            "line_bytes": 1431,
            "error_position": 1285,
            "error_type": "JSONDecodeError",
            "error": "Expecting delimiter",
        }],
        "total_lines": 274839,
        "raw_payload_redacted": True,
        "disposition": "CHANNEL_OBJECT_QUARANTINE_REQUIRED",
    })
    declaration_path = run / "DATA_INTEGRITY/RFQ_OBJECT_QUARANTINE.json"
    write_json(declaration_path, {
        "schema_version": repair.DECLARATION_SCHEMA,
        "run_id": run.name,
        "mode": "EXPLORATORY_AUTORESEARCH",
        "finding": repair.FINDING,
        "disposition": repair.DECLARATION_DISPOSITION,
        "created_after_structural_failure_before_rfq_result": True,
        "dependent_rfq_result_opened": False,
        "source_execution_commit": old_commit,
        "remaining_object_parse_policy": (
            "STRICT_NDJSON_IGNORE_ERRORS_FALSE; any additional malformed object aborts"
        ),
        "selection_rule": (
            "Quarantine the complete manifest object only when exact receipt proof binds it."
        ),
        "result_use_prohibited": (
            "The quarantined object contributes no RFQ row, lifecycle endpoint or conclusion."
        ),
        "quarantined_objects": [{
            "release_id": release_id,
            "key": key,
            "manifest_sha256": manifest_sha,
            "version_id": version_id,
            "sha256": object_sha,
            "size": object_size,
            "receipt": receipt_path.relative_to(run).as_posix(),
            "invalid_line_count": 1,
            "reason": (
                "Exact sealed bytes are malformed; no line-level salvage is permitted."
            ),
        }],
    })

    (source / "analysis.py").write_text(
        "VERSION = 2\nQUARANTINE = 'whole-object'\n", encoding="utf-8"
    )
    (source / "repair_registration.py").write_text(
        "REPAIR = 'repair-01'\n", encoding="utf-8"
    )
    new_commit = git_commit(repo, "registered repair execution")
    return {
        "repo": repo, "source": source, "run": run,
        "declaration": declaration_path, "receipt": receipt_path,
        "old_commit": old_commit, "new_commit": new_commit,
        "trial_before": trial_before, "core_before": core_before,
    }


def invoke(fixture: dict):
    return repair.repair_registration(
        fixture["run"], fixture["source"], fixture["declaration"],
        fixture["receipt"],
    )


def test_success_archives_refreezes_and_appends_exactly_two_registry_records(tmp_path):
    fixture = make_fixture(tmp_path)
    result = invoke(fixture)
    assert result["status"] == "REGISTRATION_REPAIRED_AND_REFROZEN"

    run = fixture["run"]
    manifest = json.loads((run / "RUN_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["status"] == repair.POST_REPAIR_STATUS
    repository = manifest["repository"]
    assert repository["initial_execution_commit"] == fixture["old_commit"]
    assert repository["previous_execution_commit"] == fixture["old_commit"]
    assert repository["execution_commit"] == fixture["new_commit"]
    assert repository["source_manifest_sha256"] == digest(run / "SOURCE_MANIFEST.json")
    assert repository["source_sha256s_sha256"] == digest(
        run / "SOURCE_SHA256SUMS.txt"
    )
    assert repository["initial_identity"]["source_sha256s_sha256"] == digest(
        run / "DATA_INTEGRITY/repairs/repair-01/pre_repair/SOURCE_SHA256SUMS.txt"
    )
    assert repository["previous_identity"] == repository["initial_identity"]
    assert repository["initial_identity"]["source_sha256s_sha256"] is not None
    assert repository["query_set_sha256"] == digest(run / "QUERY_SHA256SUMS.txt")
    assert (run / "queries/analysis.py").read_bytes() == (
        fixture["source"] / "analysis.py"
    ).read_bytes()

    repair_dir = run / "DATA_INTEGRITY/repairs/repair-01"
    assert (repair_dir / "pre_repair/SOURCE_MANIFEST.json").is_file()
    assert (repair_dir / "pre_repair/SOURCE_SHA256SUMS.txt").is_file()
    assert (repair_dir / "pre_repair/QUERY_SHA256SUMS.txt").is_file()
    assert (repair_dir / "pre_repair/queries/analysis.py").is_file()
    assert (repair_dir / "pre_repair" / repair.FAILURE_STATE).is_file()
    assert (repair_dir / "pre_repair" / repair.FAILURE_RESOURCE).is_file()
    assert (repair_dir / "pre_repair" / repair.FAILURE_SCRATCH_RECEIPT).is_file()
    assert (repair_dir / "pre_repair" / repair.FAILED_INPUT_IDENTITY).is_file()
    assert (repair_dir / "pre_repair" / repair.CYCLE1_DUCKDB_BINDING).is_file()

    records = manifest["data_integrity_repairs"]
    assert len(records) == 1
    record = records[0]
    assert record["rfq_result_state"] == repair.NO_RFQ_RESULT
    assert record["core_result_disposition"] == repair.CORE_DISPOSITION
    assert record["core_results_recomputed"] is False
    assert record["registration_change_class"] == repair.REGISTRATION_CHANGE_CLASS
    assert record["hypothesis_design_change"] == "NONE"
    assert (
        record["data_integrity_handling_change"]
        == repair.DATA_INTEGRITY_HANDLING_CHANGE
    )
    assert record["frozen_design_change"] == repair.LEGACY_FROZEN_DESIGN_CHANGE
    assert record["threshold_feature_test_or_hypothesis_status_changed"] is False
    assert record["previous_execution_commit"] == fixture["old_commit"]
    assert record["current_execution_commit"] == fixture["new_commit"]
    failed_input = repair_dir / "pre_repair" / repair.FAILED_INPUT_IDENTITY
    assert record["failed_input_identity_path"] == failed_input.relative_to(
        run
    ).as_posix()
    assert record["failed_input_identity_sha256"] == digest(failed_input)
    assert record["failed_input_fingerprint"] == json.loads(
        failed_input.read_text(encoding="utf-8")
    )["path_size_sha_fingerprint"]
    cycle_binding = record["cycle1_duckdb_binding"]
    assert set(cycle_binding) == {
        "active_path", "active_sha256", "archived_path", "archived_sha256",
        "schema_version", "run_id", "duckdb_path", "duckdb_bytes",
        "duckdb_sha256", "core_stage_resource_path",
        "core_stage_resource_sha256", "core_summary_path", "core_summary_sha256",
    }
    assert cycle_binding["active_sha256"] == cycle_binding["archived_sha256"]
    assert record["failed_attempt"]["failed_input_identity_sha256"] == digest(
        failed_input
    )
    assert len(record["core_result_artifacts"]) == 4
    for path_field, sha_field in (
        ("declaration_path", "declaration_sha256"),
        ("receipt_path", "receipt_sha256"),
        ("failed_state_path", "failed_state_sha256"),
        ("failed_resource_receipt_path", "failed_resource_receipt_sha256"),
        ("failed_scratch_receipt_path", "failed_scratch_receipt_sha256"),
    ):
        archived = run / record[path_field]
        assert archived.is_file()
        assert digest(archived) == record[sha_field]

    registry_after = (run / "TRIAL_REGISTRY.jsonl").read_bytes()
    assert registry_after.startswith(fixture["trial_before"])
    old_lines = fixture["trial_before"].splitlines()
    new_lines = registry_after.splitlines()
    assert len(new_lines) == len(old_lines) + 2
    appended = [json.loads(line) for line in new_lines[-2:]]
    assert [row["trial_registration_id"] for row in appended] == [
        "RFQ_FULL_STAGE_ATTEMPT_01",
        "RFQ_OBJECT_QUARANTINE_REPAIR_01",
    ]
    assert [row["record_type"] for row in appended] == [
        "STAGE_FAILURE", "DATA_INTEGRITY_REPAIR_PREREGISTRATION",
    ]
    assert all(row["result_opened"] is False for row in appended)
    assert all(
        row["failed_input_identity_sha256"] == digest(failed_input)
        and row["failed_input_fingerprint"] == record["failed_input_fingerprint"]
        and row["cycle1_duckdb_binding"] == cycle_binding
        for row in appended
    )
    preregistration = appended[1]
    assert (
        preregistration["registration_change_class"]
        == repair.REGISTRATION_CHANGE_CLASS
    )
    assert preregistration["hypothesis_design_change"] == "NONE"
    assert (
        preregistration["data_integrity_handling_change"]
        == repair.DATA_INTEGRITY_HANDLING_CHANGE
    )
    assert (
        preregistration["frozen_design_change"]
        == repair.LEGACY_FROZEN_DESIGN_CHANGE
    )
    assert (
        preregistration["threshold_feature_test_or_hypothesis_status_changed"]
        is False
    )
    assert (run / "REPORT/CYCLE1_CORE_SUMMARY.json").read_bytes() == fixture[
        "core_before"
    ]
    receipt = json.loads(
        (repair_dir / "REPAIR_REGISTRATION.json").read_text(encoding="utf-8")
    )
    assert set(record) == set(receipt) | {
        "repair_receipt_path", "repair_receipt_sha256"
    }
    assert all(record[key] == value for key, value in receipt.items())

    repeated = invoke(fixture)
    assert repeated["status"] == "REGISTRATION_REPAIR_ALREADY_APPLIED"
    assert (run / "TRIAL_REGISTRY.jsonl").read_bytes() == registry_after


def test_tampered_frozen_query_is_rejected_without_mutation(tmp_path):
    fixture = make_fixture(tmp_path)
    manifest_before = (fixture["run"] / "RUN_MANIFEST.json").read_bytes()
    registry_before = (fixture["run"] / "TRIAL_REGISTRY.jsonl").read_bytes()
    (fixture["run"] / "queries/analysis.py").write_text(
        "tampered = True\n", encoding="utf-8"
    )
    with pytest.raises(repair.RepairRegistrationError, match="query"):
        invoke(fixture)
    assert (fixture["run"] / "RUN_MANIFEST.json").read_bytes() == manifest_before
    assert (fixture["run"] / "TRIAL_REGISTRY.jsonl").read_bytes() == registry_before
    assert not (fixture["run"] / "DATA_INTEGRITY/repairs/repair-01").exists()


def test_existing_rfq_full_summary_is_rejected(tmp_path):
    fixture = make_fixture(tmp_path)
    write_json(fixture["run"] / repair.RFQ_SUMMARY, {"result": "opened"})
    manifest_before = (fixture["run"] / "RUN_MANIFEST.json").read_bytes()
    with pytest.raises(repair.RepairRegistrationError, match="summary"):
        invoke(fixture)
    assert (fixture["run"] / "RUN_MANIFEST.json").read_bytes() == manifest_before
    assert not (fixture["run"] / "DATA_INTEGRITY/repairs/repair-01").exists()


def test_dirty_research_source_is_rejected_but_unrelated_dirty_is_ignored(tmp_path):
    fixture = make_fixture(tmp_path)
    unrelated = fixture["repo"] / "unrelated-user-file.txt"
    unrelated.write_text("dirty but outside registered source\n", encoding="utf-8")
    (fixture["source"] / "analysis.py").write_text("dirty = True\n", encoding="utf-8")
    with pytest.raises(repair.RepairRegistrationError, match="committed and clean"):
        invoke(fixture)
    assert unrelated.read_text(encoding="utf-8").startswith("dirty")
    assert not (fixture["run"] / "DATA_INTEGRITY/repairs/repair-01").exists()


def test_receipt_tamper_is_rejected(tmp_path):
    fixture = make_fixture(tmp_path)
    receipt = json.loads(fixture["receipt"].read_text(encoding="utf-8"))
    receipt["observed_size"] += 1
    write_json(fixture["receipt"], receipt)
    with pytest.raises(repair.RepairRegistrationError, match="size"):
        invoke(fixture)
    assert not (fixture["run"] / "DATA_INTEGRITY/repairs/repair-01").exists()


@pytest.mark.parametrize("location", ["active", "archived"])
def test_applied_source_sha_receipt_mutation_is_rejected(tmp_path, location):
    fixture = make_fixture(tmp_path)
    invoke(fixture)
    if location == "active":
        target = fixture["run"] / "SOURCE_SHA256SUMS.txt"
    else:
        target = (
            fixture["run"]
            / "DATA_INTEGRITY/repairs/repair-01/pre_repair/SOURCE_SHA256SUMS.txt"
        )
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(
        repair.RepairRegistrationError,
        match=(
            "repository identities|archive inventory|registration artifacts|"
            "pre_repair SOURCE_MANIFEST/SOURCE_SHA"
        ),
    ):
        invoke(fixture)


def test_current_source_sha_must_match_current_manifest_row_by_row(tmp_path):
    fixture = make_fixture(tmp_path)
    invoke(fixture)
    run = fixture["run"]
    source_manifest_path = run / "SOURCE_MANIFEST.json"
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    source_manifest[0]["sha256"] = "9" * 64
    write_json(source_manifest_path, source_manifest)

    # Even a coordinated edit of the mutable top-level hash bindings cannot
    # make a SOURCE_SHA receipt that disagrees with its itemized manifest valid.
    manifest_path = run / "RUN_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    changed_sha = digest(source_manifest_path)
    manifest["repository"]["source_manifest_sha256"] = changed_sha
    manifest["data_integrity_repairs"][0][
        "current_source_manifest_sha256"
    ] = changed_sha
    write_json(manifest_path, manifest)
    with pytest.raises(
        repair.RepairRegistrationError,
        match="does not exactly match|repository identities",
    ):
        invoke(fixture)


def test_failed_input_identity_must_equal_rebuilt_manifest_union(tmp_path):
    fixture = make_fixture(tmp_path)
    identity_path = fixture["run"] / repair.FAILED_INPUT_IDENTITY
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    identity["objects"][0]["size"] += 1
    write_json(identity_path, identity)
    manifest_before = (fixture["run"] / "RUN_MANIFEST.json").read_bytes()
    with pytest.raises(
        repair.RepairRegistrationError,
        match="exact immutable manifest union",
    ):
        invoke(fixture)
    assert (fixture["run"] / "RUN_MANIFEST.json").read_bytes() == manifest_before
    assert not (fixture["run"] / "DATA_INTEGRITY/repairs/repair-01").exists()


def test_failed_state_fingerprint_must_equal_rebuilt_union(tmp_path):
    fixture = make_fixture(tmp_path)
    state_path = fixture["run"] / repair.FAILURE_STATE
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["input_fingerprint"] = "a" * 64
    write_json(state_path, state)
    with pytest.raises(repair.RepairRegistrationError, match="fingerprint"):
        invoke(fixture)
    assert not (fixture["run"] / "DATA_INTEGRITY/repairs/repair-01").exists()


def test_cycle1_binding_core_hash_tamper_is_rejected_without_reading_db(tmp_path):
    fixture = make_fixture(tmp_path)
    binding_path = fixture["run"] / repair.CYCLE1_DUCKDB_BINDING
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    binding["core_summary_sha256"] = "a" * 64
    write_json(binding_path, binding)
    with pytest.raises(repair.RepairRegistrationError, match="core summary hash"):
        invoke(fixture)
    assert not (fixture["run"] / "cache/cycle1.duckdb").exists()
    assert not (fixture["run"] / "DATA_INTEGRITY/repairs/repair-01").exists()


def test_idempotent_repair_receipt_self_binding_mutation_is_rejected(tmp_path):
    fixture = make_fixture(tmp_path)
    invoke(fixture)
    receipt = fixture["run"] / "DATA_INTEGRITY/repairs/repair-01/REPAIR_REGISTRATION.json"
    receipt.write_bytes(receipt.read_bytes() + b" ")
    with pytest.raises(repair.RepairRegistrationError, match="receipt hash"):
        invoke(fixture)


def test_idempotent_exact_archive_inventory_mutation_is_rejected(tmp_path):
    fixture = make_fixture(tmp_path)
    invoke(fixture)
    archived_input = (
        fixture["run"]
        / "DATA_INTEGRITY/repairs/repair-01/pre_repair"
        / repair.FAILED_INPUT_IDENTITY
    )
    archived_input.write_bytes(archived_input.read_bytes() + b" ")
    with pytest.raises(repair.RepairRegistrationError, match="archive inventory"):
        invoke(fixture)


def test_idempotent_active_failed_input_mutation_is_rejected(tmp_path):
    fixture = make_fixture(tmp_path)
    invoke(fixture)
    active_input = fixture["run"] / repair.FAILED_INPUT_IDENTITY
    value = json.loads(active_input.read_text(encoding="utf-8"))
    value["unique_objects"] += 1
    write_json(active_input, value)
    with pytest.raises(
        repair.RepairRegistrationError,
        match="exact immutable manifest union|identities differ",
    ):
        invoke(fixture)


def test_idempotent_active_cycle_receipt_mutation_is_rejected(tmp_path):
    fixture = make_fixture(tmp_path)
    invoke(fixture)
    active_cycle = fixture["run"] / repair.CYCLE1_DUCKDB_BINDING
    value = json.loads(active_cycle.read_text(encoding="utf-8"))
    value["sha256"] = "a" * 64
    write_json(active_cycle, value)
    with pytest.raises(
        repair.RepairRegistrationError,
        match="Cycle-1 DuckDB bindings differ",
    ):
        invoke(fixture)


def test_idempotent_core_result_inventory_mutation_is_rejected(tmp_path):
    fixture = make_fixture(tmp_path)
    invoke(fixture)
    core = fixture["run"] / "REPORT/tables/CORE_HYPOTHESIS_TESTS.json"
    core.write_bytes(core.read_bytes() + b" ")
    with pytest.raises(repair.RepairRegistrationError, match="core result inventory"):
        invoke(fixture)


def test_idempotent_trial_registry_prefix_mutation_is_rejected(tmp_path):
    fixture = make_fixture(tmp_path)
    invoke(fixture)
    registry = fixture["run"] / "TRIAL_REGISTRY.jsonl"
    payload = registry.read_bytes()
    needle = b'"result_opened":false'
    assert needle in payload
    registry.write_bytes(payload.replace(needle, b'"result_opened":true ', 1))
    with pytest.raises(repair.RepairRegistrationError, match="prefix binding"):
        invoke(fixture)
