import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).with_name("repair03_registration.py")
SPEC = importlib.util.spec_from_file_location("repair03_registration", MODULE_PATH)
repair = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(repair)


QUERY_NAMES = [
    "run_cycle1.py",
    "core_hypothesis_tests.py",
    "l2_hypothesis_stage.py",
    "rfq_trigger.py",
    "rfq_full_stage.py",
    "finalize_mission.py",
]


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        list(args), cwd=cwd, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def commit(repo: Path, message: str) -> str:
    command(repo, "git", "add", "sandbox/research/deep_autoresearch")
    command(repo, "git", "commit", "-m", message)
    return command(repo, "git", "rev-parse", "HEAD")


def identity(seed: str, query_files: list[str]) -> dict:
    return {
        "execution_commit": seed * 40,
        "source_manifest_sha256": seed * 64,
        "source_sha256s_sha256": chr(ord(seed) + 1) * 64,
        "query_set_sha256": chr(ord(seed) + 2) * 64,
        "query_files": copy.deepcopy(query_files),
    }


def make_transaction_fixture(tmp_path: Path, monkeypatch) -> dict:
    repo = tmp_path / "repo"
    source = repo / "sandbox/research/deep_autoresearch"
    source.mkdir(parents=True)
    command(repo, "git", "init")
    command(repo, "git", "config", "user.name", "Repair 03 Test")
    command(repo, "git", "config", "user.email", "repair03@example.invalid")
    for index, name in enumerate(QUERY_NAMES):
        (source / name).write_text(
            f"VERSION = 1\nINDEX = {index}\n", encoding="utf-8"
        )
    (source / "repair_registration.py").write_text(
        "REPAIR = 1\n", encoding="utf-8"
    )
    parent_commit = commit(repo, "repair-02 source")
    monkeypatch.setattr(repair, "PARENT_EXECUTION_COMMIT", parent_commit)

    run = tmp_path / "synthetic-repair03-run"
    for relative in (
        "queries", "REPORT/tables", "REPORT", "logs/resources", "tmp",
        "cache", "DATA_INTEGRITY", "DATA_INTEGRITY/repairs/repair-01",
        "DATA_INTEGRITY/repairs/repair-02",
    ):
        (run / relative).mkdir(parents=True, exist_ok=True)
    for name in QUERY_NAMES:
        (run / "queries" / name).write_bytes((source / name).read_bytes())

    (run / "SOURCE_MANIFEST.json").write_bytes(b'[{"parent":true}]\n')
    (run / "SOURCE_SHA256SUMS.txt").write_text(
        "a" * 64 + "  parent.py\n", encoding="utf-8"
    )
    query_sums = "".join(
        f"{digest(run / 'queries' / name)}  queries/{name}\n"
        for name in QUERY_NAMES
    )
    (run / "QUERY_SHA256SUMS.txt").write_text(query_sums, encoding="utf-8")
    for index, relative in enumerate(repair.base.CORE_RESULT_PATHS):
        write_json(run / relative, {"core": index})
    cycle_binding_raw = {"binding": "preserved", "sha256": "d" * 64}
    write_json(run / repair.CYCLE1_BINDING, cycle_binding_raw)
    cycle_binding = {
        "active_path": repair.CYCLE1_BINDING.as_posix(),
        "active_sha256": digest(run / repair.CYCLE1_BINDING),
        "duckdb_sha256": "d" * 64,
    }

    trial_rows = [
        {"trial_registration_id": "RFQ_FULL_STAGE_ATTEMPT_01"},
        {"trial_registration_id": "RFQ_OBJECT_QUARANTINE_REPAIR_01"},
        {"trial_registration_id": "RFQ_FULL_STAGE_ATTEMPT_02"},
        {"trial_registration_id": "RFQ_OBJECT_QUARANTINE_REPAIR_02"},
    ]
    parent_trials = b"".join(repair.base.registry_line(row) for row in trial_rows)
    (run / "TRIAL_REGISTRY.jsonl").write_bytes(parent_trials)

    query_files = [f"queries/{name}" for name in QUERY_NAMES]
    old_repository = {
        "bootstrap_commit": parent_commit,
        "execution_commit": parent_commit,
        "source_manifest_path": "SOURCE_MANIFEST.json",
        "source_manifest_sha256": digest(run / "SOURCE_MANIFEST.json"),
        "source_sha256s_sha256": digest(run / "SOURCE_SHA256SUMS.txt"),
        "query_set_sha256": digest(run / "QUERY_SHA256SUMS.txt"),
        "query_files": query_files,
        "source_tree_dirty_at_freeze": False,
        "registration_repair_id": "repair-02",
    }
    first_identity = identity("1", query_files)
    second_identity = identity("4", query_files)
    current_parent_identity = repair.base.repository_identity(old_repository)
    chain = [first_identity, second_identity, current_parent_identity]
    old_repository.update({
        "initial_identity": first_identity,
        "previous_identity": second_identity,
        "identity_history": chain,
        "initial_execution_commit": first_identity["execution_commit"],
    })
    repair01 = {"repair_id": "repair-01"}
    cumulative = [
        {"key": "raw_rfq/a", "size": 1, "sha256": "a" * 64},
        {"key": "raw_rfq/b", "size": 2, "sha256": "b" * 64},
    ]
    parent = {
        "schema_version": "sports-autoresearch-data-integrity-repair-v2",
        "repair_id": "repair-02",
        "parent_repair_id": "repair-01",
        "post_repair_status": repair.EXPECTED_STATUS,
        "current_execution_commit": parent_commit,
        "current_repository_identity": current_parent_identity,
        "repository_identity_chain": chain,
        "cumulative_quarantined_objects": cumulative,
        "cycle1_duckdb_binding": cycle_binding,
        "repair_receipt_path": (
            "DATA_INTEGRITY/repairs/repair-02/REPAIR_REGISTRATION.json"
        ),
    }
    write_json(run / parent["repair_receipt_path"], {"parent": 2})
    parent["repair_receipt_sha256"] = digest(run / parent["repair_receipt_path"])
    manifest = {
        "run_id": run.name,
        "analysis_started": True,
        "status": repair.EXPECTED_STATUS,
        "registration_state": (
            "RE_FROZEN_AFTER_DATA_INTEGRITY_REPAIR02_BEFORE_RFQ_RESULT"
        ),
        "repository": old_repository,
        "data_integrity_repairs": [repair01, parent],
    }
    write_json(run / "RUN_MANIFEST.json", manifest)

    for relative in (
        repair.STATE, repair.RESOURCE, repair.SCRATCH_RECEIPT,
        repair.INPUT_IDENTITY, repair.AUDIT, repair.AUDIT_RESOURCE,
    ):
        write_json(run / relative, {"path": relative.as_posix()})
    (run / repair.AUDIT_SOURCE).write_text(
        "# audit source\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        repair, "FAILED_SCRATCH_RECEIPT_SHA256",
        digest(run / repair.SCRATCH_RECEIPT),
    )
    monkeypatch.setattr(repair, "AUDIT_SHA256", digest(run / repair.AUDIT))
    monkeypatch.setattr(
        repair, "AUDIT_RESOURCE_SHA256", digest(run / repair.AUDIT_RESOURCE)
    )
    preserved = run / repair.PRESERVED_SCRATCH
    preserved.write_bytes(b"failed-scratch")
    scratch_receipt = {
        "sha256": digest(preserved),
        "bytes": preserved.stat().st_size,
        "disposition": "PRESERVED_RENAMED_NO_RESUME",
    }
    failed = {
        "state": {"status": repair.FAILED_STAGE_STATUS},
        "resource": {"return_code": 1},
        "input": {"selection_fingerprint_sha256": repair.SELECTION_FINGERPRINT},
        "state_sha256": digest(run / repair.STATE),
        "resource_sha256": digest(run / repair.RESOURCE),
        "input_sha256": digest(run / repair.INPUT_IDENTITY),
    }
    audit_source_sha = digest(run / repair.AUDIT_SOURCE)
    core = repair.base.core_result_inventory(run)
    monkeypatch.setattr(
        repair, "validate_parent_repair",
        lambda *_: (
            parent, copy.deepcopy(old_repository), parent_trials,
            copy.deepcopy(trial_rows), core,
        ),
    )
    monkeypatch.setattr(
        repair, "validate_failed_attempt", lambda *_: copy.deepcopy(failed)
    )
    monkeypatch.setattr(
        repair, "validate_failed_scratch", lambda *_: copy.deepcopy(scratch_receipt)
    )
    monkeypatch.setattr(
        repair, "validate_inner_payload_audit",
        lambda *_: ({"audit": 3}, {"resource": 3}, audit_source_sha),
    )
    monkeypatch.setattr(
        repair.base, "validate_cycle1_duckdb_binding",
        lambda *_: (
            copy.deepcopy(cycle_binding_raw), copy.deepcopy(cycle_binding)
        ),
    )
    monkeypatch.setattr(
        repair.base, "verify_prior_repository",
        lambda *_: (copy.deepcopy(old_repository), copy.deepcopy(QUERY_NAMES)),
    )

    (source / "rfq_full_stage.py").write_text(
        "VERSION = 2\nPARSER_CONTRACT = 'repair-03'\n", encoding="utf-8"
    )
    (source / "repair03_registration.py").write_text(
        "REPAIR = 3\n", encoding="utf-8"
    )
    current_commit = commit(repo, "repair-03 source")
    return {
        "repo": repo,
        "source": source,
        "run": run,
        "parent": parent,
        "parent_commit": parent_commit,
        "current_commit": current_commit,
        "parent_trials": parent_trials,
        "preserved": preserved,
    }


def invoke(fixture: dict):
    return repair.repair03_registration(fixture["run"], fixture["source"])


def write_snapshot_attestation(fixture: dict, **overrides) -> dict:
    _, manifest_payload, sums_payload = repair.base.build_source_receipts(
        fixture["source"], fixture["repo"]
    )
    value = {
        "schema_version": repair.SOURCE_ATTESTATION_SCHEMA,
        "run_id": fixture["run"].name,
        "mission_sha256": repair.MISSION_SHA256,
        "parent_execution_commit": fixture["parent_commit"],
        "execution_commit": fixture["current_commit"],
        "direct_parent_verified": True,
        "git_tree": "a" * 40,
        "source_relative": repair.SOURCE_RELATIVE,
        "source_tree_clean_at_attestation": True,
        "source_manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
        "source_sha256s_sha256": hashlib.sha256(sums_payload).hexdigest(),
        "commit_changed_paths": copy.deepcopy(
            repair.EXPECTED_REPAIR03_CHANGED_PATHS
        ),
        "attested_at_utc": "2026-07-15T16:00:00Z",
    }
    value.update(overrides)
    write_json(fixture["run"] / repair.SOURCE_ATTESTATION, value)
    return value


def run_snapshot(run: Path) -> dict[str, bytes]:
    return {
        path.relative_to(run).as_posix(): path.read_bytes()
        for path in sorted(run.rglob("*"))
        if path.is_file() and path.relative_to(run) != repair.RUN_LOCK
    }


FAULT_BOUNDARIES = [
    "publish_repair_dir",
    "write:SOURCE_MANIFEST.json",
    "write:SOURCE_SHA256SUMS.txt",
    "write:QUERY_SHA256SUMS.txt",
    *(f"write:queries/{name}" for name in QUERY_NAMES),
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
    assert not (
        fixture["run"] / "DATA_INTEGRITY/repairs/repair-03"
    ).exists()
    monkeypatch.setattr(repair, "_transaction_fault_hook", lambda _label: None)
    assert invoke(fixture)["status"] == "REGISTRATION_REPAIR03_REFROZEN"


def test_success_is_append_only_refrozen_and_idempotent(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    result = invoke(fixture)
    assert result["status"] == "REGISTRATION_REPAIR03_REFROZEN"
    run = fixture["run"]
    manifest = json.loads((run / "RUN_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["status"] == repair.POST_REPAIR_STATUS
    assert manifest["registration_state"] == repair.REGISTRATION_STATE
    record = manifest["data_integrity_repairs"][-1]
    assert record["newly_quarantined_objects"] == []
    assert record["coverage"] == repair._expected_coverage()
    assert record["previous_selection_fingerprint_sha256"] == (
        repair.SELECTION_FINGERPRINT
    )
    assert record["current_selection_fingerprint_sha256"] == (
        repair.SELECTION_FINGERPRINT
    )
    assert record["current_execution_commit"] == fixture["current_commit"]
    assert record["previous_execution_commit"] == fixture["parent_commit"]
    assert record["cycle1_duckdb_binding"] == (
        fixture["parent"]["cycle1_duckdb_binding"]
    )
    assert record["parser_contract"]["audited_parser_contract"] == (
        repair.AUDITED_PARSER_CONTRACT
    )
    registry = (run / "TRIAL_REGISTRY.jsonl").read_bytes()
    assert registry.startswith(fixture["parent_trials"])
    appended = [json.loads(line) for line in registry.splitlines()][-2:]
    assert [row["trial_registration_id"] for row in appended] == list(
        repair.TRIAL_IDS
    )
    assert all(row["result_opened"] is False for row in appended)
    receipt = json.loads(
        (run / record["repair_receipt_path"]).read_text(encoding="utf-8")
    )
    expected_receipt = copy.deepcopy(record)
    expected_receipt.pop("repair_receipt_path")
    expected_receipt.pop("repair_receipt_sha256")
    assert receipt == expected_receipt

    before_repeat = run_snapshot(run)
    repeated = invoke(fixture)
    assert repeated["status"] == "REGISTRATION_REPAIR03_ALREADY_APPLIED"
    assert run_snapshot(run) == before_repeat


def test_cycle1_binding_compares_normalized_wrapper_before_writes(
    tmp_path, monkeypatch
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    parent_binding = copy.deepcopy(
        fixture["parent"]["cycle1_duckdb_binding"]
    )
    write_json(fixture["run"] / repair.CYCLE1_BINDING, parent_binding)
    mismatched_wrapper = copy.deepcopy(parent_binding)
    mismatched_wrapper["active_sha256"] = "e" * 64
    monkeypatch.setattr(
        repair.base, "validate_cycle1_duckdb_binding",
        lambda *_: (copy.deepcopy(parent_binding), mismatched_wrapper),
    )
    before = run_snapshot(fixture["run"])

    with pytest.raises(
        repair.Repair03RegistrationError,
        match="active Cycle-1 DuckDB binding changed after repair-02",
    ):
        invoke(fixture)

    assert run_snapshot(fixture["run"]) == before


def test_gitless_snapshot_mode_archives_exact_attestation(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    attestation = write_snapshot_attestation(fixture)
    monkeypatch.setattr(
        repair, "validate_snapshot_parent_repository",
        lambda *_: copy.deepcopy(QUERY_NAMES),
    )
    result = repair.repair03_registration(
        fixture["run"], fixture["source"],
        repo_root=fixture["repo"], execution_commit=fixture["current_commit"],
    )
    assert result["status"] == "REGISTRATION_REPAIR03_REFROZEN"
    assert result["source_verification_mode"] == repair.SNAPSHOT_SOURCE_MODE
    manifest = json.loads(
        (fixture["run"] / "RUN_MANIFEST.json").read_text(encoding="utf-8")
    )
    record = manifest["data_integrity_repairs"][-1]
    assert record["source_verification_mode"] == repair.SNAPSHOT_SOURCE_MODE
    assert record["source_snapshot_attestation"]["git_tree"] == (
        attestation["git_tree"]
    )
    active = fixture["run"] / repair.SOURCE_ATTESTATION
    archived = fixture["run"] / record["source_snapshot_attestation_path"]
    assert archived.read_bytes() == active.read_bytes()
    assert digest(archived) == record["source_snapshot_attestation_sha256"]

    before_repeat = run_snapshot(fixture["run"])
    repeated = repair.repair03_registration(
        fixture["run"], fixture["source"],
        repo_root=fixture["repo"], execution_commit=fixture["current_commit"],
    )
    assert repeated["status"] == "REGISTRATION_REPAIR03_ALREADY_APPLIED"
    assert run_snapshot(fixture["run"]) == before_repeat


def test_snapshot_mode_requires_paired_parameters(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    with pytest.raises(repair.Repair03RegistrationError, match="requires both"):
        repair.repair03_registration(
            fixture["run"], fixture["source"], repo_root=fixture["repo"]
        )
    with pytest.raises(repair.Repair03RegistrationError, match="requires both"):
        repair.repair03_registration(
            fixture["run"], fixture["source"],
            execution_commit=fixture["current_commit"],
        )


def test_snapshot_layout_rejects_wrong_root_commit_and_symlink(tmp_path):
    root = tmp_path / "staging"
    source = root / repair.SOURCE_RELATIVE
    source.mkdir(parents=True)
    (source / "rfq_full_stage.py").write_text("X = 1\n", encoding="utf-8")
    assert repair.validate_snapshot_source_layout(
        source, root, "a" * 40
    ) == (root.resolve(), repair.SOURCE_RELATIVE, "a" * 40)
    with pytest.raises(repair.Repair03RegistrationError, match="exactly"):
        repair.validate_snapshot_source_layout(source, tmp_path, "a" * 40)
    with pytest.raises(repair.Repair03RegistrationError, match="full lowercase"):
        repair.validate_snapshot_source_layout(source, root, "A" * 40)
    (source / "linked.py").symlink_to(source / "rfq_full_stage.py")
    with pytest.raises(repair.Repair03RegistrationError, match="symlink"):
        repair.validate_snapshot_source_layout(source, root, "a" * 40)


def test_snapshot_parent_verification_binds_active_receipts_and_queries(tmp_path):
    run = tmp_path / "parent-run"
    (run / "queries").mkdir(parents=True)
    source_payload = b"PARENT = 2\n"
    source_sha = hashlib.sha256(source_payload).hexdigest()
    source_manifest = [{
        "path": f"{repair.SOURCE_RELATIVE}/rfq_full_stage.py",
        "sha256": source_sha,
        "bytes": len(source_payload),
    }]
    (run / "SOURCE_MANIFEST.json").write_bytes(
        repair.base.json_payload(source_manifest)
    )
    (run / "SOURCE_SHA256SUMS.txt").write_text(
        f"{source_sha}  rfq_full_stage.py\n", encoding="utf-8"
    )
    frozen = run / "queries/rfq_full_stage.py"
    frozen.write_bytes(source_payload)
    (run / "QUERY_SHA256SUMS.txt").write_text(
        f"{source_sha}  queries/rfq_full_stage.py\n", encoding="utf-8"
    )
    repository = {
        "execution_commit": repair.PARENT_EXECUTION_COMMIT,
        "source_tree_dirty_at_freeze": False,
        "source_manifest_path": "SOURCE_MANIFEST.json",
        "source_manifest_sha256": digest(run / "SOURCE_MANIFEST.json"),
        "source_sha256s_sha256": digest(run / "SOURCE_SHA256SUMS.txt"),
        "query_set_sha256": digest(run / "QUERY_SHA256SUMS.txt"),
        "query_files": ["queries/rfq_full_stage.py"],
    }
    assert repair.validate_snapshot_parent_repository(run, repository) == [
        "rfq_full_stage.py"
    ]
    frozen.write_bytes(b"MUTATED = True\n")
    with pytest.raises(repair.Repair03RegistrationError, match="query changed"):
        repair.validate_snapshot_parent_repository(run, repository)


@pytest.mark.parametrize(
    "override,pattern",
    [
        ({"direct_parent_verified": False}, "mismatch"),
        ({"source_manifest_sha256": "f" * 64}, "mismatch"),
        ({"execution_commit": "e" * 40}, "mismatch"),
        ({"commit_changed_paths": []}, "mismatch"),
        ({"git_tree": "NOT_A_TREE"}, "mismatch"),
    ],
)
def test_snapshot_attestation_rejects_false_or_unbound_claims(
    tmp_path, monkeypatch, override, pattern
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    write_snapshot_attestation(fixture, **override)
    _, manifest_payload, sums_payload = repair.base.build_source_receipts(
        fixture["source"], fixture["repo"]
    )
    with pytest.raises(repair.Repair03RegistrationError, match=pattern):
        repair.validate_source_snapshot_attestation(
            fixture["run"], fixture["current_commit"],
            hashlib.sha256(manifest_payload).hexdigest(),
            hashlib.sha256(sums_payload).hexdigest(),
        )


def test_concurrent_registry_suffix_is_retained(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    registry = fixture["run"] / "TRIAL_REGISTRY.jsonl"
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
    with pytest.raises(repair.Repair03RegistrationError, match="CAS failed"):
        invoke(fixture)
    assert registry.read_bytes() == fixture["parent_trials"] + suffix
    assert not (
        fixture["run"] / "DATA_INTEGRITY/repairs/repair-03"
    ).exists()


def test_two_registrars_are_serialized_before_orphan_recovery(
    tmp_path, monkeypatch
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    published = threading.Event()
    release = threading.Event()
    first_results = []
    first_errors = []

    def hold_live_transaction(label: str) -> None:
        if label == "publish_repair_dir" and not published.is_set():
            published.set()
            if not release.wait(10):
                raise RuntimeError("test timed out while holding registrar lock")

    def first_registrar() -> None:
        try:
            first_results.append(invoke(fixture))
        except BaseException as exc:  # Captured for assertion in the main thread.
            first_errors.append(exc)

    monkeypatch.setattr(repair, "_transaction_fault_hook", hold_live_transaction)
    thread = threading.Thread(target=first_registrar, daemon=True)
    thread.start()
    assert published.wait(10)
    repair_dir = fixture["run"] / "DATA_INTEGRITY/repairs/repair-03"
    assert repair_dir.is_dir()
    try:
        with pytest.raises(
            repair.Repair03RegistrationError, match="already active"
        ):
            invoke(fixture)
        assert repair_dir.is_dir()
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert first_errors == []
    assert first_results[0]["status"] == "REGISTRATION_REPAIR03_REFROZEN"


def test_run_lock_symlink_fails_before_touching_orphan_state(
    tmp_path, monkeypatch
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    repair_dir = fixture["run"] / "DATA_INTEGRITY/repairs/repair-03"
    repair_dir.mkdir()
    sentinel = repair_dir / "live-owner-sentinel"
    sentinel.write_text("do-not-recover\n", encoding="utf-8")
    lock_target = tmp_path / "lock-target"
    lock_target.write_text("untouched\n", encoding="utf-8")
    (fixture["run"] / repair.RUN_LOCK).symlink_to(lock_target)
    manifest_before = (fixture["run"] / "RUN_MANIFEST.json").read_bytes()

    with pytest.raises(repair.Repair03RegistrationError, match="lock.*unsafe"):
        invoke(fixture)
    assert sentinel.read_text(encoding="utf-8") == "do-not-recover\n"
    assert lock_target.read_text(encoding="utf-8") == "untouched\n"
    assert (fixture["run"] / "RUN_MANIFEST.json").read_bytes() == manifest_before


def test_partial_registry_write_removes_owned_prefix_and_keeps_foreign_suffix(
    tmp_path, monkeypatch
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    registry = fixture["run"] / "TRIAL_REGISTRY.jsonl"
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
    with pytest.raises(repair.Repair03RegistrationError, match="append was partial"):
        invoke(fixture)
    assert injected is True
    assert registry.read_bytes() == fixture["parent_trials"] + foreign
    assert not (
        fixture["run"] / "DATA_INTEGRITY/repairs/repair-03"
    ).exists()


def test_interrupted_registry_write_recovery_removes_only_owned_short_prefix(
    tmp_path, monkeypatch
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    registry = fixture["run"] / "TRIAL_REGISTRY.jsonl"
    foreign = b'{"foreign_after_interrupted_write":true}\n'
    real_write = repair.os.write
    injected = False

    def interrupted_registry_write(descriptor: int, payload: bytes) -> int:
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
            count = len(payload) // 4
            assert real_write(descriptor, payload[:count]) == count
            foreign_descriptor = os.open(registry, os.O_WRONLY | os.O_APPEND)
            try:
                assert real_write(foreign_descriptor, foreign) == len(foreign)
                os.fsync(foreign_descriptor)
            finally:
                os.close(foreign_descriptor)
            raise OSError("simulated interruption after short registry write")
        return real_write(descriptor, payload)

    monkeypatch.setattr(repair.os, "write", interrupted_registry_write)
    with pytest.raises(OSError, match="simulated interruption"):
        invoke(fixture)
    assert injected is True
    assert registry.read_bytes() == fixture["parent_trials"] + foreign
    assert not (
        fixture["run"] / "DATA_INTEGRITY/repairs/repair-03"
    ).exists()


def test_nonregistry_conditional_replace_does_not_overwrite_last_window_mutation(
    tmp_path, monkeypatch
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    active = fixture["run"] / "SOURCE_MANIFEST.json"
    foreign = b'{"foreign_last_window":true}\n'

    def mutate_at_conditional_replace(label: str) -> None:
        if label == "write:SOURCE_MANIFEST.json:at_conditional_replace":
            active.write_bytes(foreign)

    monkeypatch.setattr(
        repair, "_transaction_fault_hook", mutate_at_conditional_replace
    )
    with pytest.raises(
        repair.Repair03RegistrationError, match="safe rollback|compared unequal"
    ):
        invoke(fixture)
    assert active.read_bytes() == foreign
    manifest = json.loads(
        (fixture["run"] / "RUN_MANIFEST.json").read_text(encoding="utf-8")
    )
    assert len(manifest["data_integrity_repairs"]) == 2


def test_manifest_conditional_replace_does_not_overwrite_last_window_mutation(
    tmp_path, monkeypatch
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    manifest_path = fixture["run"] / "RUN_MANIFEST.json"
    foreign_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    foreign_manifest["foreign_last_window"] = True
    foreign_payload = repair.base.json_payload(foreign_manifest)

    def mutate_manifest_at_conditional_replace(label: str) -> None:
        if label == "write:RUN_MANIFEST.json:at_conditional_replace":
            manifest_path.write_bytes(foreign_payload)

    monkeypatch.setattr(
        repair, "_transaction_fault_hook", mutate_manifest_at_conditional_replace
    )
    with pytest.raises(
        repair.Repair03RegistrationError, match="safe rollback|compared unequal"
    ):
        invoke(fixture)
    assert manifest_path.read_bytes() == foreign_payload


@pytest.mark.parametrize("mutation", ["result", "evidence"])
def test_final_manifest_rechecks_no_result_and_immutable_evidence(
    tmp_path, monkeypatch, mutation
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    evidence = fixture["run"] / repair.AUDIT
    evidence_before = evidence.read_bytes()

    def mutate_final_boundary(label: str) -> None:
        if label != "final_manifest_after_cas_before_write":
            return
        if mutation == "result":
            write_json(fixture["run"] / repair.RFQ_SUMMARY, {"foreign": True})
        else:
            evidence.write_bytes(evidence_before + b"foreign-mutation")

    monkeypatch.setattr(repair, "_transaction_fault_hook", mutate_final_boundary)
    pattern = "RFQ result appeared" if mutation == "result" else "immutable evidence CAS"
    with pytest.raises(repair.Repair03RegistrationError, match=pattern):
        invoke(fixture)
    manifest = json.loads(
        (fixture["run"] / "RUN_MANIFEST.json").read_text(encoding="utf-8")
    )
    assert len(manifest["data_integrity_repairs"]) == 2
    assert not (
        fixture["run"] / "DATA_INTEGRITY/repairs/repair-03"
    ).exists()
    if mutation == "result":
        assert (fixture["run"] / repair.RFQ_SUMMARY).is_file()
    else:
        assert evidence.read_bytes() == evidence_before + b"foreign-mutation"


def test_source_snapshot_drift_before_commit_is_rejected(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    run_before = run_snapshot(fixture["run"])
    changed = False

    def mutate_after_publication(label: str) -> None:
        nonlocal changed
        if label == "publish_repair_dir" and not changed:
            changed = True
            (fixture["source"] / "rfq_full_stage.py").write_text(
                "CONCURRENT_SOURCE_DRIFT = True\n", encoding="utf-8"
            )

    monkeypatch.setattr(repair, "_transaction_fault_hook", mutate_after_publication)
    with pytest.raises(repair.Repair03RegistrationError, match="source snapshot CAS"):
        invoke(fixture)
    assert run_snapshot(fixture["run"]) == run_before
    assert not (
        fixture["run"] / "DATA_INTEGRITY/repairs/repair-03"
    ).exists()


def test_orphan_journal_recovers_on_next_invocation(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    real_recover = repair.recover_incomplete_transaction

    def fail_after_first_write(label: str) -> None:
        if label == "write:SOURCE_MANIFEST.json":
            raise RuntimeError("process interruption")

    monkeypatch.setattr(repair, "_transaction_fault_hook", fail_after_first_write)
    monkeypatch.setattr(
        repair, "recover_incomplete_transaction",
        lambda *_: (_ for _ in ()).throw(RuntimeError("rollback interrupted")),
    )
    with pytest.raises(repair.Repair03RegistrationError, match="safe rollback"):
        invoke(fixture)
    repair_dir = fixture["run"] / "DATA_INTEGRITY/repairs/repair-03"
    assert repair_dir.is_dir()

    monkeypatch.setattr(repair, "recover_incomplete_transaction", real_recover)
    monkeypatch.setattr(repair, "_transaction_fault_hook", lambda _label: None)
    assert invoke(fixture)["status"] == "REGISTRATION_REPAIR03_REFROZEN"


@pytest.mark.parametrize(
    "field",
    [
        "parser_contract_path",
        "authority_basis_path",
        "inner_payload_audit_path",
        "failed_state_path",
    ],
)
def test_idempotent_validation_rejects_evidence_mutation(
    tmp_path, monkeypatch, field
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    invoke(fixture)
    run = fixture["run"]
    manifest = json.loads((run / "RUN_MANIFEST.json").read_text(encoding="utf-8"))
    path = run / manifest["data_integrity_repairs"][-1][field]
    path.write_bytes(path.read_bytes() + b"mutation")
    with pytest.raises(repair.Repair03RegistrationError, match="mismatch|changed"):
        invoke(fixture)


def test_parser_contract_and_authority_have_exact_canonical_keys():
    contract = repair.make_parser_contract(
        "run", "2026-07-15T16:00:00Z", "a" * 64, "audit.json", "b" * 64
    )
    assert set(contract) == {
        "schema_version", "run_id", "created_at_utc", "finding",
        "mission_sha256", "selection_fingerprint_sha256",
        "retained_unique_objects", "retained_bytes", "outer_ndjson_policy",
        "expected_blank_control_markers", "expected_blank_raw_representation",
        "non_control_payload_policy", "unexpected_payload_policy", "line_salvage",
        "data_selection_change", "hypothesis_design_change",
        "registered_query_path", "registered_query_sha256", "audit_path",
        "audit_sha256",
    }
    assert contract["non_control_payload_policy"] == (
        "NONEMPTY_STRING_VALID_JSON_OBJECT_REQUIRED"
    )
    authority = repair.make_authority_basis(
        "run", "2026-07-15T16:00:00Z", "audit.json", "b" * 64
    )
    assert set(authority) == {
        "schema_version", "run_id", "recorded_at_utc", "mission_sha256",
        "authority_basis", "permitted_change",
        "operator_repair02_authorization_reused", "new_data_integrity_decision",
        "data_selection_change", "quarantine_change", "hypothesis_design_change",
        "dependent_rfq_result_opened", "prerequisite_audit_path",
        "prerequisite_audit_sha256",
    }
    assert authority["operator_repair02_authorization_reused"] is False


def test_failed_scratch_validator_binds_rename_and_no_resume(tmp_path, monkeypatch):
    run = tmp_path / "scratch-run"
    (run / "DATA_INTEGRITY").mkdir(parents=True)
    (run / "cache").mkdir()
    preserved = run / repair.PRESERVED_SCRATCH
    preserved.write_bytes(b"abc")
    scratch_sha = digest(preserved)
    mtime = "2026-07-15T15:28:30.567397717+00:00"
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
        "sha256": scratch_sha,
        "mtime_utc": mtime,
    }
    write_json(run / repair.SCRATCH_RECEIPT, receipt)
    monkeypatch.setattr(
        repair, "FAILED_SCRATCH_RECEIPT_SHA256", digest(run / repair.SCRATCH_RECEIPT)
    )
    monkeypatch.setattr(repair, "FAILED_SCRATCH_SHA256", scratch_sha)
    monkeypatch.setattr(repair, "FAILED_SCRATCH_BYTES", 3)
    monkeypatch.setattr(repair, "FAILED_SCRATCH_MTIME", mtime)
    assert repair.validate_failed_scratch(run, state) == receipt
    (run / repair.SCRATCH).write_bytes(b"new")
    with pytest.raises(repair.Repair03RegistrationError, match="still exists"):
        repair.validate_failed_scratch(run, state)


def test_inner_payload_audit_validator_binds_exact_contract(tmp_path, monkeypatch):
    run = tmp_path / "audit-run"
    (run / "DATA_INTEGRITY").mkdir(parents=True)
    (run / "logs/resources").mkdir(parents=True)
    (run / "tmp").mkdir()
    source = run / repair.AUDIT_SOURCE
    source.write_text("# exact audit\n", encoding="utf-8")
    monkeypatch.setattr(repair, "AUDIT_SOURCE_SHA256", digest(source))
    summaries = []
    consumed = []
    for index in range(repair.RETAINED_OBJECTS):
        size = 1 if index else repair.RETAINED_BYTES - repair.RETAINED_OBJECTS + 1
        lines = 1 if index else repair.RETAINED_LINES - repair.RETAINED_OBJECTS + 1
        frames = (
            1 if index else repair.EXPECTED_DATA_FRAME_ROWS - repair.RETAINED_OBJECTS + 1
        )
        key = f"raw_rfq/object-{index:03d}"
        consumed.append({"key": key, "size": size, "sha256": "a" * 64})
        summaries.append({
            "key": key,
            "identity_match": True,
            "expected_size": size,
            "observed_size": size,
            "expected_sha256": "a" * 64,
            "observed_sha256": "a" * 64,
            "total_lines": lines,
            "data_frame_rows": frames,
            "segment_receipt_rows": (
                repair.EXPECTED_SEGMENT_RECEIPT_ROWS if index == 0 else 0
            ),
            "expected_empty_control_marker_rows": (
                repair.EXPECTED_BLANK_CONTROLS if index == 0 else 0
            ),
            "marker_counts": (
                copy.deepcopy(repair.EXPECTED_AUDIT_MARKER_COUNTS)
                if index == 0 else {}
            ),
            "outer_invalid_rows": 0,
            "unexpected_inner_payload_rows": 0,
            "unexpected_details": [],
            "raw_payload_redacted": True,
        })
    write_json(run / repair.INPUT_IDENTITY, {"consumed_objects": consumed})
    input_sha = digest(run / repair.INPUT_IDENTITY)
    audit = {
        "analysis_result_opened": False,
        "audit_script_path": repair.AUDIT_SOURCE.as_posix(),
        "audit_script_sha256": digest(source),
        "bytes_scanned": repair.RETAINED_BYTES,
        "completed_at_utc": "2026-07-15T15:41:19Z",
        "data_frame_rows": repair.EXPECTED_DATA_FRAME_ROWS,
        "expected_empty_control_marker_allowlist": list(
            repair.EXPECTED_BLANK_CONTROL_MARKERS
        ),
        "expected_empty_control_marker_rows": repair.EXPECTED_BLANK_CONTROLS,
        "identity_mismatch_count": 0,
        "identity_mismatches": [],
        "input_fingerprint": repair.SELECTION_FINGERPRINT,
        "input_identity_path": repair.INPUT_IDENTITY.as_posix(),
        "input_identity_sha256": input_sha,
        "json_object_payload_marker_contract": [
            "<marker field absent>", "segment_receipt",
        ],
        "lines_scanned": repair.RETAINED_LINES,
        "marker_counts": copy.deepcopy(repair.EXPECTED_AUDIT_MARKER_COUNTS),
        "object_summaries": summaries,
        "objects_scanned": repair.RETAINED_OBJECTS,
        "outer_invalid_line_count": 0,
        "outer_invalid_object_count": 0,
        "parser_contract": copy.deepcopy(repair.AUDITED_PARSER_CONTRACT),
        "parser_contract_sha256": repair.AUDITED_PARSER_CONTRACT_SHA256,
        "raw_payload_redacted": True,
        "run_id": run.name,
        "schema_version": "rfq-inner-payload-contract-audit-v1",
        "scope": "RETAINED_OBJECT_INNER_PAYLOAD_CONTRACT_ONLY_NO_RESEARCH_RESULT",
        "segment_receipt_rows": repair.EXPECTED_SEGMENT_RECEIPT_ROWS,
        "started_at_utc": "2026-07-15T15:39:00Z",
        "status": "COMPLETE_INNER_PAYLOAD_CONTRACT_AUDIT",
        "unexpected_inner_payload_object_count": 0,
        "unexpected_inner_payload_objects": [],
        "unexpected_inner_payload_row_count": 0,
        "wall_seconds": 139.242,
        "workers": 8,
    }
    write_json(run / repair.AUDIT, audit)
    resource = {
        "schema_version": "w09-stage-resource-v1",
        "label": "rfq_inner_payload_contract_audit03_final",
        "return_code": 0,
        "command": [
            "/opt/w09/venv/bin/python",
            f"/srv/w09-research/runs/{run.name}/{repair.AUDIT_SOURCE.as_posix()}",
            "--run-dir", f"/srv/w09-research/runs/{run.name}",
            "--cache-root", "/srv/w09-research/cache", "--workers", "8",
        ],
    }
    write_json(run / repair.AUDIT_RESOURCE, resource)
    monkeypatch.setattr(repair, "AUDIT_SHA256", digest(run / repair.AUDIT))
    monkeypatch.setattr(
        repair, "AUDIT_RESOURCE_SHA256", digest(run / repair.AUDIT_RESOURCE)
    )
    validated, _, source_sha = repair.validate_inner_payload_audit(run, input_sha)
    assert validated == audit
    assert source_sha == digest(source)

    audit["parser_contract"]["data_frame_raw"] = "NONEMPTY_JSON_VALUE"
    write_json(run / repair.AUDIT, audit)
    monkeypatch.setattr(repair, "AUDIT_SHA256", digest(run / repair.AUDIT))
    with pytest.raises(repair.Repair03RegistrationError, match="exact clean"):
        repair.validate_inner_payload_audit(run, input_sha)
