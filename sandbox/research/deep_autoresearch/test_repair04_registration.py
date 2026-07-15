import copy
import hashlib
import importlib.util
import json
import os
import py_compile
import subprocess
import sys
import threading
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).with_name("repair04_registration.py")
SPEC = importlib.util.spec_from_file_location("repair04_registration", MODULE_PATH)
repair = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(repair)

PARENT_TEST_PATH = Path(__file__).with_name("test_repair03_registration.py")
PARENT_SPEC = importlib.util.spec_from_file_location(
    "repair04_parent_fixture", PARENT_TEST_PATH
)
parent_tests = importlib.util.module_from_spec(PARENT_SPEC)
assert PARENT_SPEC.loader is not None
PARENT_SPEC.loader.exec_module(parent_tests)


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_snapshot(run: Path) -> dict[str, bytes]:
    return {
        path.relative_to(run).as_posix(): path.read_bytes()
        for path in sorted(run.rglob("*"))
        if path.is_file() and path.relative_to(run) != repair.repair03.RUN_LOCK
    }


def make_transaction_fixture(tmp_path: Path, monkeypatch) -> dict:
    parent = parent_tests.make_transaction_fixture(tmp_path, monkeypatch)
    assert parent_tests.invoke(parent)["status"] == "REGISTRATION_REPAIR03_REFROZEN"
    run = parent["run"]
    source = parent["source"]
    manifest = json.loads((run / "RUN_MANIFEST.json").read_text(encoding="utf-8"))
    parent_record = manifest["data_integrity_repairs"][-1]
    parent_commit = manifest["repository"]["execution_commit"]
    monkeypatch.setattr(repair, "PARENT_EXECUTION_COMMIT", parent_commit)
    monkeypatch.setattr(
        repair,
        "QUARANTINED_BYTES",
        sum(row["size"] for row in parent_record["cumulative_quarantined_objects"]),
    )

    # Attempt-04 active evidence is kept small in transaction tests.  Dedicated
    # validator tests below exercise the complete exact schemas and values.
    state = {"status": repair.FAILED_STAGE_STATUS, "error": repair.FAILED_ERROR}
    resource = {
        "command": repair._expected_failed_command(run.name),
        "started_at_utc": "2026-07-15T16:45:24.076899Z",
        "completed_at_utc": "2026-07-15T17:00:34.075652Z",
        "wall_seconds": 909.999,
        "peak_process_tree_rss_kib_polled": 40_379_312,
        "cumulative_children_max_rss_kib": 40_429_620,
        "peak_temp_bytes_polled": repair.PRIOR_PEAK_TEMP_BYTES,
        "minimum_disk_free_bytes_polled": repair.PRIOR_MINIMUM_DISK_FREE_BYTES,
        "disk_free_before_bytes": repair.PRIOR_DISK_FREE_BEFORE_BYTES,
        "disk_free_after_bytes": 124_324_532_224,
    }
    identity = {
        "schema": "rfq-full-input-identity-v3",
        "selection_fingerprint_sha256": repair.SELECTION_FINGERPRINT,
    }
    write_json(run / repair.STATE, state)
    write_json(run / repair.RESOURCE, resource)
    write_json(run / repair.INPUT_IDENTITY, identity)
    write_json(run / repair.SCRATCH_RECEIPT, {"synthetic": True})
    write_json(run / repair.W09_ATTESTATION, {"synthetic": True})
    monkeypatch.setattr(repair, "FAILED_STATE_SHA256", digest(run / repair.STATE))
    monkeypatch.setattr(
        repair, "FAILED_RESOURCE_SHA256", digest(run / repair.RESOURCE)
    )
    monkeypatch.setattr(repair, "FAILED_INPUT_SHA256", digest(run / repair.INPUT_IDENTITY))
    monkeypatch.setattr(
        repair,
        "FAILED_SCRATCH_RECEIPT_SHA256",
        digest(run / repair.SCRATCH_RECEIPT),
    )

    preserved = run / repair.PRESERVED_SCRATCH
    preserved.write_bytes(b"attempt-04-failed-scratch")
    scratch_receipt = {
        "sha256": digest(preserved),
        "bytes": preserved.stat().st_size,
        "mtime_utc": "2026-07-15T17:00:33.148336873+00:00",
        "disposition": "PRESERVED_RENAMED_NO_RESUME",
    }
    monkeypatch.setattr(repair, "FAILED_SCRATCH_SHA256", digest(preserved))
    monkeypatch.setattr(repair, "FAILED_SCRATCH_BYTES", preserved.stat().st_size)
    monkeypatch.setattr(repair, "FAILED_SCRATCH_ORIGINAL_INODE", preserved.stat().st_ino)

    failed = {
        "state": state,
        "resource": resource,
        "input": identity,
        "state_sha256": digest(run / repair.STATE),
        "resource_sha256": digest(run / repair.RESOURCE),
        "input_sha256": digest(run / repair.INPUT_IDENTITY),
    }
    monkeypatch.setattr(
        repair, "validate_failed_attempt", lambda *_: copy.deepcopy(failed)
    )
    monkeypatch.setattr(
        repair,
        "validate_failed_scratch",
        lambda *_: copy.deepcopy(scratch_receipt),
    )
    monkeypatch.setattr(
        repair,
        "validate_w09_attestation",
        lambda *_: ({"instance_id": repair.W09_INSTANCE_ID}, digest(run / repair.W09_ATTESTATION)),
    )
    cycle_binding = copy.deepcopy(parent_record["cycle1_duckdb_binding"])
    monkeypatch.setattr(
        repair.base,
        "validate_cycle1_duckdb_binding",
        lambda *_: ({"synthetic": True}, copy.deepcopy(cycle_binding)),
    )
    current_parent_repository = copy.deepcopy(manifest["repository"])
    monkeypatch.setattr(
        repair,
        "_verify_prior_repository_provenance",
        lambda *_: (
            copy.deepcopy(current_parent_repository),
            copy.deepcopy(parent_tests.QUERY_NAMES),
        ),
    )

    # The repair-04 direct child changes exactly the operator-approved six files.
    changed = {
        "finalize_mission.py": "VERSION = 4\nRESOURCE_GATE = True\n",
        "repair04_registration.py": "REPAIR = 4\n",
        "rfq_full_stage.py": "VERSION = 4\nRESOURCE_CONTRACT = True\n",
        "test_finalize_mission.py": "TEST_REPAIR = 4\n",
        "test_repair04_registration.py": "TEST_REPAIR = 4\n",
        "test_rfq_full_stage.py": "TEST_REPAIR = 4\n",
    }
    for name, payload in changed.items():
        (source / name).write_text(payload, encoding="utf-8")
    current_commit = parent_tests.commit(parent["repo"], "repair-04 source")
    assert parent_tests.command(
        parent["repo"], "git", "diff", "--name-only", f"{parent_commit}..{current_commit}"
    ).splitlines() == repair.EXPECTED_REPAIR04_CHANGED_PATHS
    return {
        **parent,
        "parent_record": parent_record,
        "parent_commit": parent_commit,
        "current_commit": current_commit,
        "attempt04_preserved": preserved,
        "resource": resource,
    }


def invoke(fixture: dict):
    return repair.repair04_registration(fixture["run"], fixture["source"])


def test_legacy_repair_scratch_is_bound_through_archived_receipt(tmp_path):
    run = tmp_path / "legacy-receipt-run"
    legacy_scratch = run / "cache/rfq_full_scratch.attempt01_failed.duckdb"
    legacy_scratch.parent.mkdir(parents=True)
    legacy_scratch.write_bytes(b"immutable-attempt-01-scratch")
    current_scratch = run / repair.PRESERVED_SCRATCH
    current_scratch.write_bytes(b"immutable-attempt-04-scratch")

    receipt_relative = (
        "DATA_INTEGRITY/repairs/repair-01/pre_repair/"
        "DATA_INTEGRITY/RFQ_FAILED_SCRATCH_RECEIPT.json"
    )
    receipt_path = run / receipt_relative
    write_json(receipt_path, {
        "schema_version": "rfq-failed-scratch-receipt-v1",
        "preserved_scratch_path": (
            f"/srv/w09-research/runs/{run.name}/"
            "cache/rfq_full_scratch.attempt01_failed.duckdb"
        ),
        "bytes": legacy_scratch.stat().st_size,
        "sha256": digest(legacy_scratch),
    })
    repair_record = {
        "repair_id": "repair-01",
        "failed_attempt": {
            "scratch_receipt_path": receipt_relative,
            "scratch_receipt_sha256": digest(receipt_path),
        },
    }

    identities = repair._capture_preserved_scratch_identities(
        run, [repair_record]
    )
    assert set(identities) == {legacy_scratch, current_scratch}
    legacy_scratch.write_bytes(b"mutated")
    with pytest.raises(repair.Repair04RegistrationError, match="binding mismatch"):
        repair._capture_preserved_scratch_identities(run, [repair_record])


@pytest.mark.parametrize(
    ("boundary", "replacement_is_live"),
    [
        ("registry_rollback:before_temp_write", False),
        ("registry_rollback:after_temp_write_before_fsync", False),
        ("registry_rollback:after_temp_fsync_before_cas", False),
        ("registry_rollback:after_replace_before_dir_fsync", True),
        ("registry_rollback:after_dir_fsync", True),
    ],
)
def test_atomic_registry_rollback_faults_leave_complete_old_or_new(
    tmp_path, monkeypatch, boundary, replacement_is_live
):
    registry = tmp_path / "TRIAL_REGISTRY.jsonl"
    original = b'{"original":true}\n'
    owned = b'{"repair04_owned":true}\n'
    foreign = b'{"foreign_suffix":true}\n'
    expected = original + owned + foreign
    replacement = original + foreign
    registry.write_bytes(expected)

    def inject(label: str) -> None:
        if label == boundary:
            raise RuntimeError(f"fault:{label}")

    monkeypatch.setattr(repair, "_transaction_fault_hook", inject)
    with pytest.raises(RuntimeError, match="fault:registry_rollback"):
        repair._atomic_registry_replace_locked(registry, expected, replacement)
    assert registry.read_bytes() == (replacement if replacement_is_live else expected)
    assert not list(tmp_path.glob(".TRIAL_REGISTRY.jsonl.repair04-rollback-*"))


def test_atomic_registry_rollback_temp_write_failure_keeps_complete_old(
    tmp_path, monkeypatch
):
    registry = tmp_path / "TRIAL_REGISTRY.jsonl"
    expected = b'{"original":true}\n{"repair04_owned":true}\n'
    replacement = b'{"original":true}\n'
    registry.write_bytes(expected)

    def fail_after_partial_temp_write(descriptor: int, payload: bytes) -> None:
        assert os.write(descriptor, payload[:7]) == 7
        raise OSError("simulated rollback temporary write failure")

    monkeypatch.setattr(repair.repair03, "_write_all", fail_after_partial_temp_write)
    with pytest.raises(OSError, match="temporary write failure"):
        repair._atomic_registry_replace_locked(registry, expected, replacement)
    assert registry.read_bytes() == expected
    assert not list(tmp_path.glob(".TRIAL_REGISTRY.jsonl.repair04-rollback-*"))


def test_atomic_registry_rollback_replace_failure_keeps_complete_old(
    tmp_path, monkeypatch
):
    registry = tmp_path / "TRIAL_REGISTRY.jsonl"
    expected = b'{"original":true}\n{"repair04_owned":true}\n'
    replacement = b'{"original":true}\n'
    registry.write_bytes(expected)

    monkeypatch.setattr(
        repair.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(
            OSError("simulated atomic replace failure")
        ),
    )
    with pytest.raises(OSError, match="atomic replace failure"):
        repair._atomic_registry_replace_locked(registry, expected, replacement)
    assert registry.read_bytes() == expected
    assert not list(tmp_path.glob(".TRIAL_REGISTRY.jsonl.repair04-rollback-*"))


def test_atomic_registry_rollback_preserves_foreign_suffix(tmp_path):
    registry = tmp_path / "TRIAL_REGISTRY.jsonl"
    original = b'{"original":true}\n'
    owned = b'{"repair04_owned":true}\n'
    foreign = b'{"foreign_suffix":true}\n'
    registry.write_bytes(original + owned + foreign)
    repair._atomic_registry_replace_locked(
        registry,
        original + owned + foreign,
        original + foreign,
    )
    assert registry.read_bytes() == original + foreign


def test_atomic_registry_rollback_final_cas_preserves_late_foreign_bytes(
    tmp_path, monkeypatch
):
    registry = tmp_path / "TRIAL_REGISTRY.jsonl"
    expected = b'{"original":true}\n{"repair04_owned":true}\n'
    replacement = b'{"original":true}\n'
    late_foreign = b'{"late_foreign":true}\n'
    registry.write_bytes(expected)

    def mutate(label: str) -> None:
        if label == "registry_rollback:after_temp_fsync_before_cas":
            descriptor = os.open(registry, os.O_WRONLY | os.O_APPEND)
            try:
                assert os.write(descriptor, late_foreign) == len(late_foreign)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    monkeypatch.setattr(repair, "_transaction_fault_hook", mutate)
    with pytest.raises(
        repair.Repair04RegistrationError,
        match="changed during locked atomic rollback CAS",
    ):
        repair._atomic_registry_replace_locked(registry, expected, replacement)
    assert registry.read_bytes() == expected + late_foreign


def test_atomic_registry_rollback_rejects_hardlinked_active_inode(tmp_path):
    registry = tmp_path / "TRIAL_REGISTRY.jsonl"
    alias = tmp_path / "registry-alias"
    expected = b'{"original":true}\n{"repair04_owned":true}\n'
    registry.write_bytes(expected)
    os.link(registry, alias)
    with pytest.raises(
        repair.Repair04RegistrationError,
        match="changed before atomic rollback preparation",
    ):
        repair._atomic_registry_replace_locked(
            registry, expected, b'{"original":true}\n'
        )
    assert registry.read_bytes() == expected
    assert alias.read_bytes() == expected


def test_atomic_registry_rollback_preserves_owner_group_and_mode(tmp_path):
    registry = tmp_path / "TRIAL_REGISTRY.jsonl"
    expected = b'{"original":true}\n{"repair04_owned":true}\n'
    replacement = b'{"original":true}\n'
    registry.write_bytes(expected)
    registry.chmod(0o640)
    before = registry.stat()
    repair._atomic_registry_replace_locked(registry, expected, replacement)
    after = registry.stat()
    assert registry.read_bytes() == replacement
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert after.st_mode & 0o7777 == before.st_mode & 0o7777


def test_atomic_registry_rollback_rejects_symlink_without_touching_target(tmp_path):
    target = tmp_path / "foreign-target"
    target.write_bytes(b'{"foreign":true}\n')
    registry = tmp_path / "TRIAL_REGISTRY.jsonl"
    registry.symlink_to(target)
    with pytest.raises(OSError):
        repair._atomic_registry_replace_locked(
            registry, target.read_bytes(), b'{"replacement":true}\n'
        )
    assert target.read_bytes() == b'{"foreign":true}\n'


def test_registry_ownership_state_never_guesses_common_foreign_prefix():
    original = b'{"parent":true}\n'
    own_append = b'{"artifact_status":"OURS","trial":4}\n'
    foreign = b'{"artifact_status":"FOREIGN","trial":5}\n'
    current = original + foreign
    with pytest.raises(
        repair.Repair04RegistrationError,
        match="ownership is ambiguous",
    ):
        repair._derive_registry_rollback(
            original, own_append, current, "WRITE_IN_PROGRESS", 0
        )
    assert repair._derive_registry_rollback(
        original, own_append, current, "WRITE_RETURNED", 0
    ) == current


def test_registry_ownership_requires_prefix_and_exact_state_evidence():
    original = b'{"parent":true}\n'
    own_append = b'{"repair04_owned":true}\n'
    foreign = b'{"foreign":true}\n'
    with pytest.raises(
        repair.Repair04RegistrationError,
        match="unrecognized concurrent",
    ):
        repair._derive_registry_rollback(
            original,
            own_append,
            original + foreign + own_append,
            "APPEND_COMPLETE",
            len(own_append),
        )
    assert repair._derive_registry_rollback(
        original,
        own_append,
        original + own_append + foreign,
        "APPEND_COMPLETE",
        len(own_append),
    ) == original + foreign
    reported = len(own_append) // 2
    assert repair._derive_registry_rollback(
        original,
        own_append,
        original + own_append[:reported] + foreign,
        "WRITE_RETURNED",
        reported,
    ) == original + foreign


FAULT_BOUNDARIES = [
    "publish_repair_dir",
    "write:SOURCE_MANIFEST.json",
    "write:SOURCE_SHA256SUMS.txt",
    "write:QUERY_SHA256SUMS.txt",
    *(f"write:queries/{name}" for name in parent_tests.QUERY_NAMES),
    "trial_append_after_cas_before_write",
    "write:TRIAL_REGISTRY.jsonl",
    "write:RUN_MANIFEST.json",
]


@pytest.mark.parametrize("boundary", FAULT_BOUNDARIES)
def test_every_transaction_boundary_rolls_back(tmp_path, monkeypatch, boundary):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    before = run_snapshot(fixture["run"])

    def inject(label: str) -> None:
        if label == boundary:
            raise RuntimeError(f"fault:{label}")

    monkeypatch.setattr(repair, "_transaction_fault_hook", inject)
    with pytest.raises(RuntimeError, match="fault:"):
        invoke(fixture)
    assert run_snapshot(fixture["run"]) == before
    assert not (fixture["run"] / "DATA_INTEGRITY/repairs/repair-04").exists()
    monkeypatch.setattr(repair, "_transaction_fault_hook", lambda _label: None)
    assert invoke(fixture)["status"] == "REGISTRATION_REPAIR04_REFROZEN"


def test_success_is_append_only_refrozen_and_idempotent(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    run = fixture["run"]
    trial_before = (run / "TRIAL_REGISTRY.jsonl").read_bytes()
    result = invoke(fixture)
    assert result["status"] == "REGISTRATION_REPAIR04_REFROZEN"
    manifest = json.loads((run / "RUN_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["status"] == repair.POST_REPAIR_STATUS
    assert manifest["registration_state"] == repair.REGISTRATION_STATE
    assert [row["repair_id"] for row in manifest["data_integrity_repairs"]] == [
        "repair-01", "repair-02", "repair-03", "repair-04",
    ]
    record = manifest["data_integrity_repairs"][-1]
    assert record["parser_contract"] == fixture["parent_record"]["parser_contract"]
    assert record["resource_contract"]["current_runtime"] == repair.RUNTIME_CONTRACT
    assert record["failed_attempt"]["error"] == repair.FAILED_ERROR
    assert record["failed_attempt"]["preserved_scratch_original_inode"] == (
        fixture["attempt04_preserved"].stat().st_ino
    )
    trial_after = (run / "TRIAL_REGISTRY.jsonl").read_bytes()
    assert trial_after.startswith(trial_before)
    suffix = [json.loads(line) for line in trial_after[len(trial_before):].splitlines()]
    assert [row["trial_registration_id"] for row in suffix] == list(repair.TRIAL_IDS)
    snapshot = run_snapshot(run)
    again = invoke(fixture)
    assert again["status"] == "REGISTRATION_REPAIR04_ALREADY_APPLIED"
    assert run_snapshot(run) == snapshot


def test_resource_contract_and_authority_exact_shapes():
    evidence = {
        "failed_state_path": "state.json",
        "failed_state_sha256": "a" * 64,
        "failed_resource_receipt_path": "resource.json",
        "failed_resource_receipt_sha256": "b" * 64,
        "failed_input_identity_path": "input.json",
        "failed_input_identity_sha256": "c" * 64,
        "failed_scratch_receipt_path": "scratch.json",
        "failed_scratch_receipt_sha256": "d" * 64,
    }
    contract = repair.make_resource_contract(
        "run", "2026-07-15T18:00:00Z", evidence
    )
    assert set(contract) == {
        "schema_version", "run_id", "created_at_utc", "mission_sha256",
        "parent_repair_id", "finding", "failure_disposition", "failure_phase",
        "change_class", "failed_attempt", "w09", "previous_runtime",
        "current_runtime", "memory_safety", "disk_safety", "expected_command",
        "fresh_scratch_required", "resume_allowed",
        "failed_scratch_preservation_required", "expected_success_resource",
        "data_selection_change", "parser_contract_change", "query_semantics_change",
        "quarantine_change", "hypothesis_design_change",
    }
    assert contract["previous_runtime"] == repair.PREVIOUS_RUNTIME_CONTRACT
    assert contract["current_runtime"] == repair.RUNTIME_CONTRACT
    assert contract["expected_command"] == repair.expected_retry_command("run")
    assert contract["expected_command"][1] == (
        "/srv/w09-research/runs/run/queries/rfq_full_stage.py"
    )
    assert repair._expected_failed_command("run")[1] == (
        "/srv/w09-research/runs/run/source/rfq_full_stage.py"
    )
    assert contract["memory_safety"][
        "memory_limit_percent_of_memtotal_rounded_2dp"
    ] == 69.49
    assert contract["disk_safety"][
        "failed_scratch_deletion_allowed"
    ] is False
    authority = repair.make_authority_basis(
        "run", "2026-07-15T18:00:00Z", "contract.json", "e" * 64
    )
    assert set(authority) == {
        "schema_version", "run_id", "recorded_at_utc", "mission_sha256",
        "authority_class", "permitted_change", "resource_contract_path",
        "resource_contract_sha256", "existing_w09_instance_id",
        "new_instance_spend_authorized", "instance_resize_authorized",
        "data_selection_change", "parser_contract_change", "query_semantics_change",
        "quarantine_change", "hypothesis_design_change",
        "dependent_rfq_result_opened",
    }
    assert authority["authority_class"] == repair.AUTHORITY_CLASS


def _exact_failed_attempt_fixture(tmp_path: Path, monkeypatch):
    run = tmp_path / "exact-attempt04"
    for relative in ("REPORT/tables", "logs/resources", "DATA_INTEGRITY", "cache"):
        (run / relative).mkdir(parents=True, exist_ok=True)
    parser = {
        "path": "DATA_INTEGRITY/repairs/repair-03/RFQ_INNER_PAYLOAD_PARSER_CONTRACT.json",
        "schema_version": repair.repair03.PARSER_CONTRACT_SCHEMA,
        "sha256": "a" * 64,
    }
    parent = {
        "parser_contract_path": parser["path"],
        "parser_contract_sha256": parser["sha256"],
        "registered_rfq_query_sha256": "b" * 64,
        "repair_receipt_path": "DATA_INTEGRITY/repairs/repair-03/REPAIR_REGISTRATION.json",
        "repair_receipt_sha256": "c" * 64,
        "cycle1_duckdb_binding": {"active_sha256": "d" * 64},
    }
    repair_chain = [
        {"repair_id": "repair-01"},
        {"repair_id": "repair-02"},
        {
            "repair_id": "repair-03",
            "registration_path": parent["repair_receipt_path"],
            "registration_sha256": parent["repair_receipt_sha256"],
            "parser_contract_path": parent["parser_contract_path"],
            "parser_contract_sha256": parent["parser_contract_sha256"],
        },
    ]
    failed_bindings = [{"attempt_id": str(index)} for index in range(3)]
    gap = {"contiguous_gap_count": 1}
    state = {
        "error": repair.FAILED_ERROR,
        "error_type": repair.FAILED_ERROR_TYPE,
        "expected_success_resource": {
            "label": "rfq_full_stage_repair03",
            "path": "logs/resources/rfq_full_stage_repair03.json",
        },
        "failed_at_utc": "2026-07-15T17:00:32Z",
        "failed_attempt_bindings": failed_bindings,
        "inner_payload_parser_contract": parser,
        "input_fingerprint": repair.SELECTION_FINGERPRINT,
        "next_required_authority": repair.NEXT_REQUIRED_AUTHORITY,
        "quarantine_gap_plan": gap,
        "registered_rfq_query_sha256": parent["registered_rfq_query_sha256"],
        "repair_chain": repair_chain,
        "resume": False,
        "run_id": run.name,
        "schema": "rfq-full-stage-state-v1",
        "scratch": f"/srv/w09-research/runs/{run.name}/{repair.SCRATCH.as_posix()}",
        "started_at_utc": "2026-07-15T16:45:59Z",
        "status": repair.FAILED_STAGE_STATUS,
    }
    resource = {
        "command": repair._expected_failed_command(run.name),
        "completed_at_utc": "2026-07-15T17:00:34.075652Z",
        "cost_rate_usd_per_hour": 0.4713,
        "cpu_hours": 0.520393,
        "cpu_system_seconds": 59.735,
        "cpu_user_seconds": 1813.679,
        "cumulative_children_max_rss_kib": 40_429_620,
        "disk_free_after_bytes": 124_324_532_224,
        "disk_free_before_bytes": repair.PRIOR_DISK_FREE_BEFORE_BYTES,
        "estimated_compute_cost_usd": 0.119134,
        "label": "rfq_full_stage_repair03",
        "minimum_disk_free_bytes_polled": repair.PRIOR_MINIMUM_DISK_FREE_BYTES,
        "peak_process_tree_rss_kib_polled": 40_379_312,
        "peak_stage_cache_bytes_polled": 114_093_269_135,
        "peak_temp_bytes_polled": repair.PRIOR_PEAK_TEMP_BYTES,
        "poll_samples": 182,
        "poll_seconds": 5.0,
        "return_code": 1,
        "rss_note": "sampled",
        "s3_bytes_read_by_analysis": 0,
        "s3_note": "local cache",
        "schema_version": "w09-stage-resource-v1",
        "started_at_utc": "2026-07-15T16:45:24.076899Z",
        "wall_seconds": 909.999,
    }
    consumed = [
        {"key": f"raw_rfq/{index:03d}", "size": 1, "sha256": "e" * 64}
        for index in range(repair.RETAINED_OBJECTS)
    ]
    identity = {
        "consumed_bytes": repair.RETAINED_BYTES,
        "consumed_logical_bindings": repair.RETAINED_LOGICAL_BINDINGS,
        "consumed_object_set_sha256": repair.RETAINED_FINGERPRINT,
        "consumed_objects": consumed,
        "consumed_unique_objects": repair.RETAINED_OBJECTS,
        "coverage_status": "PARTIAL_OBJECT_COVERAGE_QUARANTINED",
        "cycle1_duckdb_binding": parent["cycle1_duckdb_binding"],
        "deduplicated_overlapping_objects": 12,
        "expected_success_resource": state["expected_success_resource"],
        "failed_attempt_binding": None,
        "failed_attempt_bindings": failed_bindings,
        "full_object_coverage": False,
        "inner_payload_parser_contract": parser,
        "line_salvage": False,
        "logical_manifest_bindings_total": repair.TOTAL_LOGICAL_BINDINGS,
        "manifest_object_set_sha256": repair.FULL_FINGERPRINT,
        "quarantine_details": [{"key": "a"}, {"key": "b"}],
        "quarantine_gap_plan": gap,
        "quarantine_reasons": ["a", "b"],
        "quarantined_bytes": repair.QUARANTINED_BYTES,
        "quarantined_logical_bindings": repair.QUARANTINED_LOGICAL_BINDINGS,
        "quarantined_object_set_sha256": repair.QUARANTINED_FINGERPRINT,
        "quarantined_unique_objects": repair.QUARANTINED_OBJECTS,
        "registered_rfq_query_sha256": parent["registered_rfq_query_sha256"],
        "release_ids": ["r1", "r2"],
        "releases": [{"release_id": "r1"}, {"release_id": "r2"}],
        "repair_chain": repair_chain,
        "run_id": run.name,
        "schema": "rfq-full-input-identity-v3",
        "selection_fingerprint_sha256": repair.SELECTION_FINGERPRINT,
        "unique_bytes_total": repair.TOTAL_BYTES,
        "unique_objects_total": repair.TOTAL_OBJECTS,
        "whole_object_quarantine": True,
    }
    write_json(run / repair.STATE, state)
    write_json(run / repair.RESOURCE, resource)
    write_json(run / repair.INPUT_IDENTITY, identity)
    monkeypatch.setattr(repair, "FAILED_STATE_SHA256", digest(run / repair.STATE))
    monkeypatch.setattr(repair, "FAILED_RESOURCE_SHA256", digest(run / repair.RESOURCE))
    monkeypatch.setattr(repair, "FAILED_INPUT_SHA256", digest(run / repair.INPUT_IDENTITY))
    monkeypatch.setattr(
        repair.repair02, "object_fingerprint", lambda _rows: repair.RETAINED_FINGERPRINT
    )
    return run, parent, state, resource, identity


def test_failed_attempt_validator_binds_exact_oom_command_and_v3_input(
    tmp_path, monkeypatch
):
    run, parent, state, resource, identity = _exact_failed_attempt_fixture(
        tmp_path, monkeypatch
    )
    result = repair.validate_failed_attempt(run, parent)
    assert result["state"] == state
    assert result["resource"] == resource
    assert result["input"] == identity
    state["error"] = state["error"].replace("37.2 GiB", "37.3 GiB", 1)
    write_json(run / repair.STATE, state)
    monkeypatch.setattr(repair, "FAILED_STATE_SHA256", digest(run / repair.STATE))
    with pytest.raises(repair.Repair04RegistrationError, match="exact DuckDB OOM"):
        repair.validate_failed_attempt(run, parent)


def test_failed_scratch_validator_binds_inode_and_no_resume(tmp_path, monkeypatch):
    run = tmp_path / "scratch-run"
    (run / "DATA_INTEGRITY").mkdir(parents=True)
    (run / "cache").mkdir()
    preserved = run / repair.PRESERVED_SCRATCH
    preserved.write_bytes(b"abc")
    state = {
        "scratch": f"/srv/w09-research/runs/{run.name}/{repair.SCRATCH.as_posix()}"
    }
    receipt = {
        "schema_version": "rfq-failed-scratch-receipt-v1",
        "run_id": run.name,
        "original_scratch_path": state["scratch"],
        "preserved_scratch_path": str(
            Path(state["scratch"]).with_name(repair.PRESERVED_SCRATCH.name)
        ),
        "input_fingerprint": repair.SELECTION_FINGERPRINT,
        "disposition": "PRESERVED_RENAMED_NO_RESUME",
        "resume_allowed": False,
        "bytes": 3,
        "sha256": digest(preserved),
        "mtime_utc": repair.FAILED_SCRATCH_MTIME,
    }
    write_json(run / repair.SCRATCH_RECEIPT, receipt)
    monkeypatch.setattr(
        repair, "FAILED_SCRATCH_RECEIPT_SHA256", digest(run / repair.SCRATCH_RECEIPT)
    )
    monkeypatch.setattr(repair, "FAILED_SCRATCH_SHA256", digest(preserved))
    monkeypatch.setattr(repair, "FAILED_SCRATCH_BYTES", 3)
    monkeypatch.setattr(repair, "FAILED_SCRATCH_ORIGINAL_INODE", preserved.stat().st_ino)
    assert repair.validate_failed_scratch(run, state) == receipt
    (run / repair.SCRATCH).write_bytes(b"new")
    with pytest.raises(repair.Repair04RegistrationError, match="still exists"):
        repair.validate_failed_scratch(run, state)


def test_snapshot_attestation_binds_direct_parent_and_exact_changed_paths(
    tmp_path, monkeypatch
):
    run = tmp_path / "snapshot-run"
    (run / "DATA_INTEGRITY").mkdir(parents=True)
    monkeypatch.setattr(repair, "PARENT_EXECUTION_COMMIT", "1" * 40)
    value = {
        "schema_version": repair.SOURCE_ATTESTATION_SCHEMA,
        "run_id": run.name,
        "mission_sha256": repair.MISSION_SHA256,
        "parent_execution_commit": repair.PARENT_EXECUTION_COMMIT,
        "execution_commit": "2" * 40,
        "direct_parent_verified": True,
        "git_tree": "3" * 40,
        "source_relative": repair.SOURCE_RELATIVE,
        "source_tree_clean_at_attestation": True,
        "source_manifest_sha256": "4" * 64,
        "source_sha256s_sha256": "5" * 64,
        "commit_changed_paths": copy.deepcopy(repair.EXPECTED_REPAIR04_CHANGED_PATHS),
        "attested_at_utc": "2026-07-15T18:00:00Z",
    }
    write_json(run / repair.SOURCE_ATTESTATION, value)
    attested, attested_sha = repair.validate_source_snapshot_attestation(
        run, "2" * 40, "4" * 64, "5" * 64
    )
    assert attested == value
    assert attested_sha == digest(run / repair.SOURCE_ATTESTATION)
    value["commit_changed_paths"] = list(reversed(value["commit_changed_paths"]))
    write_json(run / repair.SOURCE_ATTESTATION, value)
    with pytest.raises(repair.Repair04RegistrationError, match="attestation mismatch"):
        repair.validate_source_snapshot_attestation(
            run, "2" * 40, "4" * 64, "5" * 64
        )


def test_repair04_registration_rejects_gitless_snapshot_proof_before_mutation(
    tmp_path,
):
    run = tmp_path / "run"
    source = tmp_path / "snapshot" / repair.SOURCE_RELATIVE
    source.mkdir(parents=True)
    before = run_snapshot(tmp_path)
    with pytest.raises(
        repair.Repair04RegistrationError,
        match="clean local Git commit proof",
    ):
        repair.repair04_registration(
            run,
            source,
            repo_root=tmp_path / "snapshot",
            execution_commit="2" * 40,
        )
    assert run_snapshot(tmp_path) == before


def test_locked_internal_api_also_rejects_gitless_snapshot_proof(tmp_path):
    run = tmp_path / "run"
    source = tmp_path / "snapshot" / repair.SOURCE_RELATIVE
    source.mkdir(parents=True)
    with pytest.raises(
        repair.Repair04RegistrationError,
        match="clean local Git commit proof",
    ):
        repair._repair04_registration_locked(
            run,
            source,
            repo_root=tmp_path / "snapshot",
            execution_commit="2" * 40,
        )


def test_source_only_sibling_loader_ignores_timestamp_valid_pyc(
    tmp_path, monkeypatch
):
    source = tmp_path / "dependency.py"
    clean = b"VALUE = 'clean-value'\n"
    malicious = b"VALUE = 'evil!-value'\n"
    assert len(clean) == len(malicious)
    timestamp_ns = 1_700_000_000 * 1_000_000_000
    source.write_bytes(malicious)
    os.utime(source, ns=(timestamp_ns, timestamp_ns))
    py_compile.compile(
        str(source),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
    )
    source.write_bytes(clean)
    os.utime(source, ns=(timestamp_ns, timestamp_ns))

    legacy_spec = importlib.util.spec_from_file_location(
        "legacy_dependency", source
    )
    assert legacy_spec is not None and legacy_spec.loader is not None
    legacy = importlib.util.module_from_spec(legacy_spec)
    legacy_spec.loader.exec_module(legacy)
    assert legacy.VALUE == "evil!-value"

    monkeypatch.setattr(repair, "__file__", str(tmp_path / "registrar.py"))
    loaded = repair._load_sibling_source("dependency")
    assert loaded.VALUE == "clean-value"


def test_cli_removes_source_directory_before_shadowable_stdlib_imports(
    tmp_path,
):
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    for name in (
        "repair_registration.py",
        "repair02_registration.py",
        "repair03_registration.py",
        "repair04_registration.py",
    ):
        (isolated / name).write_bytes(MODULE_PATH.with_name(name).read_bytes())
    marker = isolated / "shadow-executed"
    (isolated / "json.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        "raise RuntimeError('shadow json executed')\n",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, str(isolated / "repair04_registration.py"), "--help"],
        cwd=isolated,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert completed.returncode == 0, completed.stderr
    assert not marker.exists()


def test_strict_git_environment_discards_caller_git_injection(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/attacker/repository")
    monkeypatch.setenv("GIT_INDEX_FILE", "/attacker/index")
    monkeypatch.setenv("GIT_NO_REPLACE_OBJECTS", "0")
    environment = repair._provenance_git_env()
    assert "GIT_DIR" not in environment
    assert "GIT_INDEX_FILE" not in environment
    assert environment["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"


def test_replace_refs_are_rejected_before_registration(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    parent_tests.command(
        fixture["repo"],
        "git",
        "replace",
        fixture["current_commit"],
        fixture["parent_commit"],
    )
    strict_parents = repair._provenance_git(
        fixture["repo"],
        "rev-list",
        "--parents",
        "-n",
        "1",
        fixture["current_commit"],
    ).split()
    assert strict_parents == [fixture["current_commit"], fixture["parent_commit"]]
    with pytest.raises(repair.Repair04RegistrationError, match="refs/replace"):
        invoke(fixture)


def test_nonempty_info_grafts_is_rejected_before_registration(
    tmp_path, monkeypatch
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    graft_path = Path(
        parent_tests.command(
            fixture["repo"], "git", "rev-parse", "--git-path", "info/grafts"
        )
    )
    if not graft_path.is_absolute():
        graft_path = fixture["repo"] / graft_path
    graft_path.parent.mkdir(parents=True, exist_ok=True)
    graft_path.write_text(
        f"{fixture['current_commit']} {fixture['parent_commit']}\n",
        encoding="ascii",
    )
    with pytest.raises(repair.Repair04RegistrationError, match="info/grafts"):
        invoke(fixture)


def test_shallow_repository_is_rejected_before_registration(
    tmp_path, monkeypatch
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    (fixture["repo"] / ".git/shallow").write_text(
        fixture["current_commit"] + "\n", encoding="ascii"
    )
    assert parent_tests.command(
        fixture["repo"], "git", "rev-parse", "--is-shallow-repository"
    ) == "true"
    with pytest.raises(
        repair.Repair04RegistrationError, match="shallow repositories"
    ):
        invoke(fixture)


@pytest.mark.parametrize("index_flag", ["--assume-unchanged", "--skip-worktree"])
@pytest.mark.parametrize("after_registration", [False, True])
def test_hidden_index_flags_cannot_register_or_pass_idempotence(
    tmp_path, monkeypatch, index_flag, after_registration
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    if after_registration:
        assert invoke(fixture)["status"] == "REGISTRATION_REPAIR04_REFROZEN"
    relative = f"{repair.SOURCE_RELATIVE}/rfq_full_stage.py"
    parent_tests.command(
        fixture["repo"], "git", "update-index", index_flag, relative
    )
    source = fixture["source"] / "rfq_full_stage.py"
    source.write_bytes(source.read_bytes() + b"UNCOMMITTED = True\n")
    assert parent_tests.command(
        fixture["repo"],
        "git",
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        repair.SOURCE_RELATIVE,
    ) == ""
    with pytest.raises(
        repair.Repair04RegistrationError,
        match="assume-unchanged|skip-worktree|working source bytes",
    ):
        invoke(fixture)


def test_ignored_extra_source_file_is_not_a_clean_head_binding(
    tmp_path, monkeypatch
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    relative = f"{repair.SOURCE_RELATIVE}/ignored_extra.py"
    exclude = fixture["repo"] / ".git/info/exclude"
    exclude.write_text(
        exclude.read_text(encoding="utf-8") + relative + "\n",
        encoding="utf-8",
    )
    (fixture["source"] / "ignored_extra.py").write_text(
        "UNCOMMITTED = True\n", encoding="utf-8"
    )
    assert parent_tests.command(
        fixture["repo"],
        "git",
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        repair.SOURCE_RELATIVE,
    ) == ""
    with pytest.raises(
        repair.Repair04RegistrationError,
        match="file set does not exactly match Git HEAD",
    ):
        invoke(fixture)


def test_committed_source_symlink_is_rejected(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    target = fixture["source"] / "test_repair04_registration.py"
    target.unlink()
    target.symlink_to("test_finalize_mission.py")
    parent_tests.command(fixture["repo"], "git", "add", repair.SOURCE_RELATIVE)
    parent_tests.command(fixture["repo"], "git", "commit", "--amend", "--no-edit")
    with pytest.raises(
        repair.Repair04RegistrationError,
        match="symlink or submodule|contains a symlink",
    ):
        invoke(fixture)


def test_committed_source_submodule_is_rejected(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    subrepo = tmp_path / "subrepo"
    subrepo.mkdir()
    parent_tests.command(subrepo, "git", "init")
    parent_tests.command(subrepo, "git", "config", "user.name", "Repair 04 Test")
    parent_tests.command(
        subrepo, "git", "config", "user.email", "repair04@example.invalid"
    )
    (subrepo / "payload.py").write_text("SUBMODULE = True\n", encoding="utf-8")
    parent_tests.command(subrepo, "git", "add", "payload.py")
    parent_tests.command(subrepo, "git", "commit", "-m", "submodule source")
    subcommit = parent_tests.command(subrepo, "git", "rev-parse", "HEAD")

    relative = f"{repair.SOURCE_RELATIVE}/repair04_registration.py"
    target = fixture["source"] / "repair04_registration.py"
    target.unlink()
    parent_tests.command(
        tmp_path,
        "git",
        "-c",
        "protocol.file.allow=always",
        "clone",
        str(subrepo),
        str(target),
    )
    parent_tests.command(
        fixture["repo"],
        "git",
        "update-index",
        "--add",
        "--cacheinfo",
        "160000",
        subcommit,
        relative,
    )
    parent_tests.command(fixture["repo"], "git", "commit", "--amend", "--no-edit")
    with pytest.raises(
        repair.Repair04RegistrationError,
        match="symlink or submodule",
    ):
        invoke(fixture)


def test_commit_scope_disables_rename_detection_that_can_hide_a_source_path(
    tmp_path, monkeypatch
):
    repo = tmp_path / "scope-repo"
    repo.mkdir()
    parent_tests.command(repo, "git", "init")
    parent_tests.command(repo, "git", "config", "user.name", "Repair 04 Test")
    parent_tests.command(
        repo, "git", "config", "user.email", "repair04@example.invalid"
    )
    parent_tests.command(repo, "git", "config", "diff.renames", "true")
    new_paths = {
        f"{repair.SOURCE_RELATIVE}/repair04_registration.py",
        f"{repair.SOURCE_RELATIVE}/test_repair04_registration.py",
    }
    for relative in repair.EXPECTED_REPAIR04_CHANGED_PATHS:
        if relative in new_paths:
            continue
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("VERSION = 3\n", encoding="utf-8")
    donor = repo / "sandbox/research/repair04_donor.py"
    donor.parent.mkdir(parents=True, exist_ok=True)
    donor.write_text("REPAIR = 4\n", encoding="utf-8")
    parent_tests.command(repo, "git", "add", ".")
    parent_tests.command(repo, "git", "commit", "-m", "repair-03 parent")
    parent_commit = parent_tests.command(repo, "git", "rev-parse", "HEAD")
    monkeypatch.setattr(repair, "PARENT_EXECUTION_COMMIT", parent_commit)

    for relative in repair.EXPECTED_REPAIR04_CHANGED_PATHS:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative.endswith("/repair04_registration.py"):
            path.write_bytes(donor.read_bytes())
        else:
            path.write_text("VERSION = 4\n", encoding="utf-8")
    donor.unlink()
    parent_tests.command(repo, "git", "add", "-A")
    parent_tests.command(repo, "git", "commit", "-m", "concealed rename")
    current_commit = parent_tests.command(repo, "git", "rev-parse", "HEAD")

    rename_aware = parent_tests.command(
        repo,
        "git",
        "diff",
        "--name-only",
        f"{parent_commit}..{current_commit}",
    ).splitlines()
    assert rename_aware == repair.EXPECTED_REPAIR04_CHANGED_PATHS
    with pytest.raises(
        repair.Repair04RegistrationError,
        match="changed-path scope mismatch",
    ):
        repair._validate_git_commit_scope(repo, current_commit)


def test_commit_scope_requires_exactly_one_parent(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    tree = parent_tests.command(
        fixture["repo"], "git", "rev-parse", f"{fixture['current_commit']}^{{tree}}"
    )
    merge_commit = parent_tests.command(
        fixture["repo"],
        "git",
        "commit-tree",
        tree,
        "-p",
        fixture["parent_commit"],
        "-p",
        fixture["current_commit"],
        "-m",
        "synthetic merge child",
    )
    with pytest.raises(
        repair.Repair04RegistrationError,
        match="direct repair-03 child",
    ):
        repair._validate_git_commit_scope(fixture["repo"], merge_commit)


def test_final_manifest_rechecks_no_result_and_immutable_evidence(
    tmp_path, monkeypatch
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    evidence = fixture["run"] / repair.STATE
    before = run_snapshot(fixture["run"])

    def mutate(label: str) -> None:
        if label == "final_manifest_after_cas_before_write":
            evidence.write_bytes(evidence.read_bytes() + b"foreign")

    monkeypatch.setattr(repair, "_transaction_fault_hook", mutate)
    with pytest.raises(repair.Repair04RegistrationError, match="immutable evidence CAS"):
        invoke(fixture)
    expected = dict(before)
    expected[repair.STATE.as_posix()] += b"foreign"
    assert run_snapshot(fixture["run"]) == expected


def test_two_registrars_are_serialized_before_recovery(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    outcomes = []

    def pause(label: str) -> None:
        if label == "publish_repair_dir" and not entered.is_set():
            entered.set()
            assert release.wait(10)

    monkeypatch.setattr(repair, "_transaction_fault_hook", pause)

    def first() -> None:
        outcomes.append(invoke(fixture))

    thread = threading.Thread(target=first)
    thread.start()
    assert entered.wait(10)
    with pytest.raises(repair.Repair04RegistrationError, match="already active"):
        invoke(fixture)
    release.set()
    thread.join(10)
    assert not thread.is_alive()
    assert outcomes[0]["status"] == "REGISTRATION_REPAIR04_REFROZEN"


def test_run_lock_symlink_fails_before_orphan_touch(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    lock = fixture["run"] / repair.repair03.RUN_LOCK
    lock.unlink(missing_ok=True)
    target = fixture["run"] / "foreign-lock"
    target.write_text("foreign", encoding="utf-8")
    lock.symlink_to(target)
    before = run_snapshot(fixture["run"])
    with pytest.raises(repair.Repair04RegistrationError, match="lock"):
        invoke(fixture)
    assert run_snapshot(fixture["run"]) == before


def test_concurrent_registry_suffix_is_retained(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    registry = fixture["run"] / "TRIAL_REGISTRY.jsonl"
    original = registry.read_bytes()
    suffix = b'{"concurrent_suffix":true}\n'

    def append_at_locked_boundary(label: str) -> None:
        if label == "trial_append_after_cas_before_write":
            descriptor = os.open(registry, os.O_WRONLY | os.O_APPEND)
            try:
                os.write(descriptor, suffix)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    monkeypatch.setattr(repair, "_transaction_fault_hook", append_at_locked_boundary)
    with pytest.raises(repair.Repair04RegistrationError, match="CAS failed"):
        invoke(fixture)
    assert registry.read_bytes() == original + suffix
    assert not (fixture["run"] / "DATA_INTEGRITY/repairs/repair-04").exists()


def test_partial_registry_write_removes_owned_prefix_and_keeps_foreign_suffix(
    tmp_path, monkeypatch
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    registry = fixture["run"] / "TRIAL_REGISTRY.jsonl"
    original = registry.read_bytes()
    foreign = b'{"foreign_after_short_write":true}\n'
    real_write = repair.os.write
    injected = False

    def short_registry_write(descriptor: int, payload: bytes) -> int:
        nonlocal injected
        descriptor_stat = os.fstat(descriptor)
        registry_stat = registry.stat()
        if (
            not injected
            and (descriptor_stat.st_dev, descriptor_stat.st_ino)
            == (registry_stat.st_dev, registry_stat.st_ino)
            and len(payload) > 8
        ):
            injected = True
            count = len(payload) // 3
            assert real_write(descriptor, payload[:count]) == count
            foreign_descriptor = os.open(registry, os.O_WRONLY | os.O_APPEND)
            try:
                assert real_write(foreign_descriptor, foreign) == len(foreign)
                os.fsync(foreign_descriptor)
            finally:
                os.close(foreign_descriptor)
            return count
        return real_write(descriptor, payload)

    monkeypatch.setattr(repair.os, "write", short_registry_write)
    with pytest.raises(repair.Repair04RegistrationError, match="append was partial"):
        invoke(fixture)
    assert injected is True
    assert registry.read_bytes() == original + foreign
    assert not (fixture["run"] / "DATA_INTEGRITY/repairs/repair-04").exists()


def test_interrupted_unreported_registry_write_preserves_ambiguous_bytes(
    tmp_path, monkeypatch
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    registry = fixture["run"] / "TRIAL_REGISTRY.jsonl"
    original = registry.read_bytes()
    foreign = b'{"foreign_after_interrupted_write":true}\n'
    real_write = repair.os.write
    injected = False
    written_prefix = b""

    def interrupted_registry_write(descriptor: int, payload: bytes) -> int:
        nonlocal injected, written_prefix
        descriptor_stat = os.fstat(descriptor)
        registry_stat = registry.stat()
        if (
            not injected
            and (descriptor_stat.st_dev, descriptor_stat.st_ino)
            == (registry_stat.st_dev, registry_stat.st_ino)
            and len(payload) > 8
        ):
            injected = True
            count = len(payload) // 4
            written_prefix = payload[:count]
            assert real_write(descriptor, written_prefix) == count
            foreign_descriptor = os.open(registry, os.O_WRONLY | os.O_APPEND)
            try:
                assert real_write(foreign_descriptor, foreign) == len(foreign)
                os.fsync(foreign_descriptor)
            finally:
                os.close(foreign_descriptor)
            raise OSError("simulated interruption after short registry write")
        return real_write(descriptor, payload)

    monkeypatch.setattr(repair.os, "write", interrupted_registry_write)
    with pytest.raises(
        repair.Repair04RegistrationError,
        match="safe rollback was blocked.*ownership is ambiguous",
    ):
        invoke(fixture)
    assert injected is True
    assert registry.read_bytes() == original + written_prefix + foreign
    assert (fixture["run"] / "DATA_INTEGRITY/repairs/repair-04").is_dir()


def test_recovery_after_atomic_replace_crash_is_idempotent(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    registry = fixture["run"] / "TRIAL_REGISTRY.jsonl"
    original = registry.read_bytes()

    def crash_transaction_and_rollback(label: str) -> None:
        if label == "write:TRIAL_REGISTRY.jsonl":
            raise RuntimeError("force transaction rollback")
        if label == "registry_rollback:after_replace_before_dir_fsync":
            raise RuntimeError("simulated process loss after registry replace")

    monkeypatch.setattr(
        repair, "_transaction_fault_hook", crash_transaction_and_rollback
    )
    with pytest.raises(
        repair.Repair04RegistrationError, match="safe rollback was blocked"
    ):
        invoke(fixture)
    repair_dir = fixture["run"] / "DATA_INTEGRITY/repairs/repair-04"
    progress = json.loads(
        (repair_dir / repair.REGISTRY_APPEND_PROGRESS).read_text(encoding="utf-8")
    )
    assert progress["rollback_state"] == "PREPARED"
    assert registry.read_bytes() == original

    monkeypatch.setattr(repair, "_transaction_fault_hook", lambda _label: None)
    assert invoke(fixture)["status"] == "REGISTRATION_REPAIR04_REFROZEN"


@pytest.mark.parametrize(
    "boundary",
    [
        "registry_rollback:after_temp_fsync_before_cas",
        "registry_rollback:after_replace_before_dir_fsync",
    ],
)
def test_short_write_atomic_rollback_survives_process_loss(
    tmp_path, monkeypatch, boundary
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    registry = fixture["run"] / "TRIAL_REGISTRY.jsonl"
    original = registry.read_bytes()
    foreign = b'{"foreign_after_short_write_crash":true}\n'
    real_write = repair.os.write
    real_recover = repair.recover_incomplete_transaction
    injected_write = False
    injected_fault = False
    owned_prefix = b""

    def short_registry_write(descriptor: int, payload: bytes) -> int:
        nonlocal injected_write, owned_prefix
        descriptor_stat = os.fstat(descriptor)
        registry_stat = registry.stat()
        if (
            not injected_write
            and (descriptor_stat.st_dev, descriptor_stat.st_ino)
            == (registry_stat.st_dev, registry_stat.st_ino)
            and len(payload) > 8
        ):
            injected_write = True
            owned_prefix = payload[: len(payload) // 3]
            assert real_write(descriptor, owned_prefix) == len(owned_prefix)
            foreign_descriptor = os.open(registry, os.O_WRONLY | os.O_APPEND)
            try:
                assert real_write(foreign_descriptor, foreign) == len(foreign)
                os.fsync(foreign_descriptor)
            finally:
                os.close(foreign_descriptor)
            return len(owned_prefix)
        return real_write(descriptor, payload)

    def crash_once(label: str) -> None:
        nonlocal injected_fault
        if label == boundary and not injected_fault:
            injected_fault = True
            raise RuntimeError(f"fault:{label}")

    monkeypatch.setattr(repair.os, "write", short_registry_write)
    monkeypatch.setattr(repair, "_transaction_fault_hook", crash_once)
    monkeypatch.setattr(
        repair,
        "recover_incomplete_transaction",
        lambda *_: (_ for _ in ()).throw(RuntimeError("simulated process stop")),
    )
    with pytest.raises(
        repair.Repair04RegistrationError, match="safe rollback was blocked"
    ):
        invoke(fixture)
    assert injected_write is True
    assert injected_fault is True
    assert registry.read_bytes() in {
        original + owned_prefix + foreign,
        original + foreign,
    }

    monkeypatch.setattr(repair, "recover_incomplete_transaction", real_recover)
    monkeypatch.setattr(repair, "_transaction_fault_hook", lambda _label: None)
    repair_dir = fixture["run"] / "DATA_INTEGRITY/repairs/repair-04"
    assert real_recover(fixture["run"], repair_dir) == ["TRIAL_REGISTRY.jsonl"]
    assert registry.read_bytes() == original + foreign
    assert not repair_dir.exists()


def test_recovery_tombstone_rename_is_retryable(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)

    def crash_after_tombstone(label: str) -> None:
        if label == "write:TRIAL_REGISTRY.jsonl":
            raise RuntimeError("force transaction rollback")
        if label == "registry_recovery:after_tombstone_publish":
            raise RuntimeError("simulated process loss after tombstone rename")

    monkeypatch.setattr(repair, "_transaction_fault_hook", crash_after_tombstone)
    with pytest.raises(
        repair.Repair04RegistrationError, match="safe rollback was blocked"
    ):
        invoke(fixture)
    repair_dir = fixture["run"] / "DATA_INTEGRITY/repairs/repair-04"
    tombstone = repair._recovery_tombstone(repair_dir)
    assert not repair_dir.exists()
    assert tombstone.is_dir()

    monkeypatch.setattr(repair, "_transaction_fault_hook", lambda _label: None)
    assert invoke(fixture)["status"] == "REGISTRATION_REPAIR04_REFROZEN"
    assert not os.path.lexists(tombstone)


def test_partial_tombstone_cleanup_is_retryable(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    real_cleanup = repair._cleanup_recovery_tombstone
    interrupted = False

    def interrupt_cleanup(repair_dir: Path) -> None:
        nonlocal interrupted
        tombstone = repair._recovery_tombstone(repair_dir)
        if tombstone.is_dir() and not interrupted:
            interrupted = True
            victim = next(path for path in tombstone.rglob("*") if path.is_file())
            victim.unlink()
            raise OSError("simulated partial tombstone deletion")
        real_cleanup(repair_dir)

    def force_rollback(label: str) -> None:
        if label == "write:TRIAL_REGISTRY.jsonl":
            raise RuntimeError("force transaction rollback")

    monkeypatch.setattr(repair, "_cleanup_recovery_tombstone", interrupt_cleanup)
    monkeypatch.setattr(repair, "_transaction_fault_hook", force_rollback)
    with pytest.raises(
        repair.Repair04RegistrationError, match="safe rollback was blocked"
    ):
        invoke(fixture)
    repair_dir = fixture["run"] / "DATA_INTEGRITY/repairs/repair-04"
    tombstone = repair._recovery_tombstone(repair_dir)
    assert interrupted is True
    assert tombstone.is_dir()

    monkeypatch.setattr(repair, "_cleanup_recovery_tombstone", real_cleanup)
    monkeypatch.setattr(repair, "_transaction_fault_hook", lambda _label: None)
    assert invoke(fixture)["status"] == "REGISTRATION_REPAIR04_REFROZEN"
    assert not os.path.lexists(tombstone)


def test_foreign_tombstone_identity_is_never_deleted(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    repair_dir = fixture["run"] / "DATA_INTEGRITY/repairs/repair-04"
    foreign = repair_dir.with_name(
        f".{repair_dir.name}.rollback-complete-{'0' * 64}"
    )
    foreign.mkdir()
    sentinel = foreign / "sentinel"
    sentinel.write_text("foreign", encoding="utf-8")
    with pytest.raises(
        repair.Repair04RegistrationError, match="foreign repair-04 rollback tombstone"
    ):
        invoke(fixture)
    assert sentinel.read_text(encoding="utf-8") == "foreign"


def test_nonregistry_cas_does_not_overwrite_last_window_mutation(
    tmp_path, monkeypatch
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    active = fixture["run"] / "SOURCE_MANIFEST.json"
    foreign = b'{"foreign_last_window":true}\n'

    def mutate(label: str) -> None:
        if label == "write:SOURCE_MANIFEST.json:at_conditional_replace":
            active.write_bytes(foreign)

    monkeypatch.setattr(repair, "_transaction_fault_hook", mutate)
    with pytest.raises(
        repair.Repair04RegistrationError, match="safe rollback|compared unequal"
    ):
        invoke(fixture)
    assert active.read_bytes() == foreign
    manifest = json.loads(
        (fixture["run"] / "RUN_MANIFEST.json").read_text(encoding="utf-8")
    )
    assert len(manifest["data_integrity_repairs"]) == 3


def test_manifest_cas_does_not_overwrite_last_window_mutation(
    tmp_path, monkeypatch
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    manifest_path = fixture["run"] / "RUN_MANIFEST.json"
    foreign_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    foreign_manifest["foreign_last_window"] = True
    foreign_payload = repair.base.json_payload(foreign_manifest)

    def mutate(label: str) -> None:
        if label == "write:RUN_MANIFEST.json:at_conditional_replace":
            manifest_path.write_bytes(foreign_payload)

    monkeypatch.setattr(repair, "_transaction_fault_hook", mutate)
    with pytest.raises(
        repair.Repair04RegistrationError, match="safe rollback|compared unequal"
    ):
        invoke(fixture)
    assert manifest_path.read_bytes() == foreign_payload


def test_final_manifest_rechecks_result_absence(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)

    def mutate(label: str) -> None:
        if label == "final_manifest_after_cas_before_write":
            write_json(fixture["run"] / repair.RFQ_SUMMARY, {"foreign": True})

    monkeypatch.setattr(repair, "_transaction_fault_hook", mutate)
    with pytest.raises(repair.Repair04RegistrationError, match="RFQ result appeared"):
        invoke(fixture)
    assert (fixture["run"] / repair.RFQ_SUMMARY).is_file()


def test_orphan_journal_recovers_on_next_invocation(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    real_recover = repair.recover_incomplete_transaction

    def fail_after_first_write(label: str) -> None:
        if label == "write:SOURCE_MANIFEST.json":
            raise RuntimeError("process interruption")

    monkeypatch.setattr(repair, "_transaction_fault_hook", fail_after_first_write)
    monkeypatch.setattr(
        repair,
        "recover_incomplete_transaction",
        lambda *_: (_ for _ in ()).throw(RuntimeError("rollback interrupted")),
    )
    with pytest.raises(repair.Repair04RegistrationError, match="safe rollback"):
        invoke(fixture)
    repair_dir = fixture["run"] / "DATA_INTEGRITY/repairs/repair-04"
    assert repair_dir.is_dir()
    monkeypatch.setattr(repair, "recover_incomplete_transaction", real_recover)
    monkeypatch.setattr(repair, "_transaction_fault_hook", lambda _label: None)
    assert invoke(fixture)["status"] == "REGISTRATION_REPAIR04_REFROZEN"


@pytest.mark.parametrize(
    "field",
    ["resource_contract_path", "authority_basis_path", "failed_state_path"],
)
def test_idempotent_validation_rejects_bound_evidence_mutation(
    tmp_path, monkeypatch, field
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    invoke(fixture)
    manifest = json.loads(
        (fixture["run"] / "RUN_MANIFEST.json").read_text(encoding="utf-8")
    )
    path = fixture["run"] / manifest["data_integrity_repairs"][-1][field]
    path.write_bytes(path.read_bytes() + b"mutation")
    with pytest.raises(repair.Repair04RegistrationError, match="mismatch|changed"):
        invoke(fixture)
