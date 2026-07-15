import copy
import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).with_name("repair02_registration.py")
SPEC = importlib.util.spec_from_file_location("repair02_registration", MODULE_PATH)
repair = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(repair)


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
        list(args), cwd=cwd, text=True, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def commit(repo: Path, message: str) -> str:
    command(repo, "git", "add", "sandbox/research/deep_autoresearch")
    command(repo, "git", "commit", "-m", message)
    return command(repo, "git", "rev-parse", "HEAD")


def make_transaction_fixture(tmp_path: Path, monkeypatch) -> dict:
    repo = tmp_path / "repo"
    source = repo / "sandbox/research/deep_autoresearch"
    source.mkdir(parents=True)
    command(repo, "git", "init")
    command(repo, "git", "config", "user.name", "Repair 02 Test")
    command(repo, "git", "config", "user.email", "repair02@example.invalid")
    query_names = [
        "run_cycle1.py", "core_hypothesis_tests.py", "l2_hypothesis_stage.py",
        "rfq_trigger.py", "rfq_full_stage.py", "finalize_mission.py",
    ]
    for index, name in enumerate(query_names):
        (source / name).write_text(f"VERSION = 1\nINDEX = {index}\n", encoding="utf-8")
    (source / "repair_registration.py").write_text("REPAIR = 1\n", encoding="utf-8")
    parent_commit = commit(repo, "repair-01 source")
    monkeypatch.setattr(repair, "PARENT_EXECUTION_COMMIT", parent_commit)

    run = tmp_path / "synthetic-repair02-run"
    (run / "queries").mkdir(parents=True)
    for name in query_names:
        (run / "queries" / name).write_bytes((source / name).read_bytes())
    for relative in (
        "REPORT/tables", "REPORT", "logs/resources", "DATA_INTEGRITY",
        "DATA_INTEGRITY/repairs/repair-01",
    ):
        (run / relative).mkdir(parents=True, exist_ok=True)

    old_source_payload = b'[{"parent":true}]\n'
    (run / "SOURCE_MANIFEST.json").write_bytes(old_source_payload)
    (run / "SOURCE_SHA256SUMS.txt").write_text(
        "a" * 64 + "  parent.py\n", encoding="utf-8"
    )
    query_sums = "".join(
        f"{digest(run / 'queries' / name)}  queries/{name}\n" for name in query_names
    )
    (run / "QUERY_SHA256SUMS.txt").write_text(query_sums, encoding="utf-8")
    for index, relative in enumerate(repair.base.CORE_RESULT_PATHS):
        write_json(run / relative, {"core": index})
    write_json(run / repair.CYCLE1_BINDING, {"binding": "preserved"})

    base_trials = b"".join(
        (
            json.dumps(
                {
                    "trial_id": trial_id,
                    "family": "rfq",
                    "status": "REGISTERED",
                    "result_opened": False,
                },
                sort_keys=True,
            )
            + "\n"
        ).encode()
        for trial_id in repair.base.RFQ_TRIAL_IDS
    )
    parent_trials = base_trials + (
        json.dumps({"trial_registration_id": "RFQ_FULL_STAGE_ATTEMPT_01"}, sort_keys=True)
        + "\n"
        + json.dumps(
            {"trial_registration_id": "RFQ_OBJECT_QUARANTINE_REPAIR_01"},
            sort_keys=True,
        )
        + "\n"
    ).encode()
    (run / "TRIAL_REGISTRY.jsonl").write_bytes(parent_trials)

    old_repository = {
        "bootstrap_commit": parent_commit,
        "execution_commit": parent_commit,
        "source_manifest_path": "SOURCE_MANIFEST.json",
        "source_manifest_sha256": digest(run / "SOURCE_MANIFEST.json"),
        "source_sha256s_sha256": digest(run / "SOURCE_SHA256SUMS.txt"),
        "query_set_sha256": digest(run / "QUERY_SHA256SUMS.txt"),
        "query_files": [f"queries/{name}" for name in query_names],
        "source_tree_dirty_at_freeze": False,
        "registration_repair_id": "repair-01",
    }
    initial_identity = {
        **repair.base.repository_identity(old_repository),
        "execution_commit": "1" * 40,
        "source_manifest_sha256": "1" * 64,
        "source_sha256s_sha256": "2" * 64,
        "query_set_sha256": "3" * 64,
    }
    old_repository["initial_identity"] = initial_identity
    old_repository["previous_identity"] = initial_identity
    old_repository["initial_execution_commit"] = "1" * 40

    parent_record = {
        "schema_version": "sports-autoresearch-data-integrity-repair-v1",
        "repair_id": "repair-01",
        "current_execution_commit": parent_commit,
        "current_source_manifest_sha256": old_repository["source_manifest_sha256"],
        "current_source_sha256s_sha256": old_repository["source_sha256s_sha256"],
        "current_query_set_sha256": old_repository["query_set_sha256"],
        "current_repository_identity": repair.base.repository_identity(old_repository),
        "repair_receipt_path": (
            "DATA_INTEGRITY/repairs/repair-01/REPAIR_REGISTRATION.json"
        ),
        "receipt_sha256": "4" * 64,
        "trial_registry": {
            "current_sha256": hashlib.sha256(parent_trials).hexdigest(),
            "current_bytes": len(parent_trials),
        },
        "cycle1_duckdb_binding": {"binding": "preserved"},
    }
    parent_receipt_path = run / parent_record["repair_receipt_path"]
    write_json(parent_receipt_path, {"synthetic_parent_receipt": True})
    parent_record["repair_receipt_sha256"] = digest(parent_receipt_path)
    resume_record = {
        "session_id": "session-02-repair02-authorized",
        "evidence_path": repair.SESSION_RESUME.as_posix(),
        "evidence_sha256": "5" * 64,
    }
    manifest = {
        "run_id": run.name,
        "analysis_started": True,
        "status": repair.EXPECTED_STATUS,
        "repository": old_repository,
        "data_integrity_repairs": [parent_record],
        "session_resumes": [resume_record],
    }
    write_json(run / "RUN_MANIFEST.json", manifest)

    for relative in (
        repair.STATE, repair.RESOURCE, repair.SCRATCH_RECEIPT,
        repair.FAILED_INPUT_IDENTITY, repair.STRUCTURAL_AUDIT,
        repair.STRUCTURAL_RESOURCE, repair.BLOCKER, repair.SESSION_RESUME,
    ):
        write_json(run / relative, {"path": relative.as_posix()})
    declaration = run / "DATA_INTEGRITY/RFQ_OBJECT_QUARANTINE_02.json"
    receipt = run / "DATA_INTEGRITY/RFQ_MALFORMED_OBJECT_RECEIPT_02.json"
    authorization = run / "DATA_INTEGRITY/REPAIR_02_USER_AUTHORIZATION.json"
    write_json(declaration, {"declaration": 2})
    write_json(receipt, {"receipt": 2})
    write_json(authorization, {"authorization": 2})

    second = {
        "invalid_line_count": 1,
        "key": "a-second-object",
        "manifest_sha256": "6" * 64,
        "reason": "no line-level salvage is permitted",
        "receipt": receipt.relative_to(run).as_posix(),
        "release_id": "release",
        "sha256": "7" * 64,
        "size": 20,
        "version_id": "v2",
    }
    first = {
        "invalid_line_count": 1,
        "key": "b-first-object",
        "manifest_sha256": "6" * 64,
        "reason": "no line-level salvage is permitted",
        "receipt": "DATA_INTEGRITY/RFQ_MALFORMED_OBJECT_RECEIPT.json",
        "release_id": "release",
        "sha256": "8" * 64,
        "size": 30,
        "version_id": "v1",
    }
    original = {
        "objects": [second, first, {"key": "c-retained", "size": 50,
                                     "sha256": "9" * 64,
                                     "bound_release_ids": ["release"]}],
    }
    # The transaction test mocks evidence interpretation, while exercising all
    # filesystem, append-only, receipt and repository refreeze operations.
    monkeypatch.setattr(
        repair, "validate_parent_repair",
        lambda *_: (parent_record, original, [first], repair.canonical_hash(parent_record)),
    )
    monkeypatch.setattr(
        repair, "validate_session_resume",
        lambda *_: (resume_record, digest(run / repair.SESSION_RESUME)),
    )
    monkeypatch.setattr(
        repair, "validate_authorization",
        lambda *_: ({"authority_source": "operator_chat_message"},
                    authorization.relative_to(run).as_posix(), digest(authorization)),
    )
    monkeypatch.setattr(
        repair, "validate_malformed_receipt",
        lambda *_: ({"receipt": 2}, second, receipt.relative_to(run).as_posix(),
                    digest(receipt)),
    )
    monkeypatch.setattr(
        repair, "validate_declaration",
        lambda *_: ({"declaration": 2}, [second, first],
                    declaration.relative_to(run).as_posix(), digest(declaration)),
    )
    monkeypatch.setattr(
        repair, "validate_active_attempt_identity",
        lambda *_: ({"identity": 2}, "a" * 64, []),
    )
    monkeypatch.setattr(repair, "validate_attempt_and_audit", lambda *_: {})
    coverage = {
        "status": "PARTIAL_OBJECT_COVERAGE_QUARANTINED",
        "full_unique_objects": 3,
        "full_logical_manifest_bindings": 3,
        "full_unique_bytes": 100,
        "retained_unique_objects": 1,
        "retained_logical_manifest_bindings": 1,
        "retained_bytes": 50,
        "quarantined_unique_objects": 2,
        "quarantined_logical_manifest_bindings": 2,
        "quarantined_bytes": 50,
        "full_object_set_sha256": "a" * 64,
        "retained_object_set_sha256": "b" * 64,
        "quarantined_object_set_sha256": "c" * 64,
        "retained_selection_fingerprint_sha256": "d" * 64,
        "whole_object_quarantine": True,
        "line_salvage": False,
    }
    monkeypatch.setattr(repair, "coverage_record", lambda *_: (coverage, []))
    monkeypatch.setattr(
        repair.base,
        "verify_prior_repository",
        lambda *_: (copy.deepcopy(old_repository), query_names),
    )

    (source / "rfq_full_stage.py").write_text("VERSION = 2\n", encoding="utf-8")
    (source / "repair02_registration.py").write_text("REPAIR = 2\n", encoding="utf-8")
    current_commit = commit(repo, "repair-02 source")
    return {
        "repo": repo,
        "source": source,
        "run": run,
        "declaration": declaration,
        "receipt": receipt,
        "authorization": authorization,
        "parent_commit": parent_commit,
        "current_commit": current_commit,
        "parent_trials": parent_trials,
        "coverage": coverage,
    }


def invoke(fixture: dict):
    return repair.repair02_registration(
        fixture["run"], fixture["source"], fixture["declaration"],
        fixture["receipt"], fixture["authorization"],
    )


def run_snapshot(run: Path) -> dict[str, bytes]:
    return {
        path.relative_to(run).as_posix(): path.read_bytes()
        for path in sorted(run.rglob("*"))
        if path.is_file()
    }


TRANSACTION_BOUNDARIES = [
    "publish_repair_dir",
    "write:SOURCE_MANIFEST.json",
    "write:SOURCE_SHA256SUMS.txt",
    "write:QUERY_SHA256SUMS.txt",
    "write:queries/run_cycle1.py",
    "write:queries/core_hypothesis_tests.py",
    "write:queries/l2_hypothesis_stage.py",
    "write:queries/rfq_trigger.py",
    "write:queries/rfq_full_stage.py",
    "write:queries/finalize_mission.py",
    "trial_append_after_cas_before_write",
    "write:TRIAL_REGISTRY.jsonl",
    "write:RUN_MANIFEST.json",
]


def test_selection_fingerprint_contract_is_exact():
    result = repair.retained_selection_fingerprint(
        repair.FULL_FINGERPRINT,
        repair.RETAINED_OBJECT_FINGERPRINT,
        repair.QUARANTINED_OBJECT_FINGERPRINT,
        "f1311f2824742f7d2da6ba3416b802b0fa5a844642f1e631f566cc2a5e1ec07d",
        [
            "dca39e5781578fd667b83eaa6286d6b7762b2fa22404fe89c55a66409619a4ac",
            "7890f86055b6c730a15acdbfee776496de5b8bb86cf55195f66f882ffb661b90",
        ],
    )
    assert result == "8b310c37f3989770d1f53a058e5ef396e1eb5c9c3d24d07ac87bd9d8aee5b9e1"


@pytest.mark.parametrize("boundary", TRANSACTION_BOUNDARIES)
def test_every_transaction_boundary_rolls_back_and_can_retry(
    tmp_path, monkeypatch, boundary
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    before = run_snapshot(fixture["run"])
    reached: list[str] = []

    def inject(label: str) -> None:
        reached.append(label)
        if label == boundary:
            raise RuntimeError(f"injected failure at {label}")

    monkeypatch.setattr(repair, "_transaction_fault_hook", inject)
    with pytest.raises(RuntimeError, match="injected failure"):
        invoke(fixture)
    assert boundary in reached
    assert run_snapshot(fixture["run"]) == before
    assert not (fixture["run"] / "DATA_INTEGRITY/repairs/repair-02").exists()

    monkeypatch.setattr(repair, "_transaction_fault_hook", lambda _label: None)
    assert invoke(fixture)["status"] == "REGISTRATION_REPAIR02_REFROZEN"


def test_orphan_journal_recovers_after_process_level_rollback_interruption(
    tmp_path, monkeypatch
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    original_recover = repair.recover_incomplete_transaction

    def inject(label: str) -> None:
        if label == "write:SOURCE_MANIFEST.json":
            raise RuntimeError("simulated process interruption")

    monkeypatch.setattr(repair, "_transaction_fault_hook", inject)
    monkeypatch.setattr(
        repair,
        "recover_incomplete_transaction",
        lambda *_: (_ for _ in ()).throw(RuntimeError("rollback process died")),
    )
    with pytest.raises(repair.Repair02RegistrationError, match="rollback was blocked"):
        invoke(fixture)
    repair_dir = fixture["run"] / "DATA_INTEGRITY/repairs/repair-02"
    assert repair_dir.is_dir()
    assert (repair_dir / repair.TRANSACTION_JOURNAL).is_file()

    monkeypatch.setattr(repair, "recover_incomplete_transaction", original_recover)
    monkeypatch.setattr(repair, "_transaction_fault_hook", lambda _label: None)
    assert invoke(fixture)["status"] == "REGISTRATION_REPAIR02_REFROZEN"


def test_trial_registry_concurrent_suffix_is_never_overwritten(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    run = fixture["run"]
    before = run_snapshot(run)
    suffix = b'{"concurrent_suffix":true}\n'
    injected = False

    def append_concurrently(label: str) -> None:
        nonlocal injected
        if label == "write:SOURCE_MANIFEST.json" and not injected:
            registry = run / "TRIAL_REGISTRY.jsonl"
            registry.write_bytes(registry.read_bytes() + suffix)
            injected = True

    monkeypatch.setattr(repair, "_transaction_fault_hook", append_concurrently)
    with pytest.raises(repair.Repair02RegistrationError, match="CAS"):
        invoke(fixture)
    assert injected
    after = run_snapshot(run)
    expected = dict(before)
    expected["TRIAL_REGISTRY.jsonl"] += suffix
    assert after == expected
    assert not (run / "DATA_INTEGRITY/repairs/repair-02").exists()


def test_trial_append_between_cas_and_write_is_detected_and_preserved(
    tmp_path, monkeypatch
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    run = fixture["run"]
    before = run_snapshot(run)
    suffix = b'{"raced_after_cas_before_write":true}\n'
    injected = False

    def race_at_exact_boundary(label: str) -> None:
        nonlocal injected
        if label == "trial_append_after_cas_before_write" and not injected:
            descriptor = os.open(run / "TRIAL_REGISTRY.jsonl", os.O_WRONLY | os.O_APPEND)
            try:
                assert os.write(descriptor, suffix) == len(suffix)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            injected = True

    monkeypatch.setattr(repair, "_transaction_fault_hook", race_at_exact_boundary)
    with pytest.raises(repair.Repair02RegistrationError, match="CAS"):
        invoke(fixture)
    assert injected
    expected = dict(before)
    expected["TRIAL_REGISTRY.jsonl"] += suffix
    assert run_snapshot(run) == expected
    assert not (run / "DATA_INTEGRITY/repairs/repair-02").exists()


def test_success_is_append_only_refrozen_and_idempotent(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    result = invoke(fixture)
    assert result["status"] == "REGISTRATION_REPAIR02_REFROZEN"
    run = fixture["run"]
    manifest = json.loads((run / "RUN_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["status"] == repair.POST_REPAIR_STATUS
    assert [row["repair_id"] for row in manifest["data_integrity_repairs"]] == [
        "repair-01", "repair-02",
    ]
    record = manifest["data_integrity_repairs"][1]
    assert record["schema_version"] == repair.REPAIR_SCHEMA
    assert record["parent_repair_id"] == "repair-01"
    assert record["retry_requirement"] == repair.RETRY_REQUIREMENT
    assert record["coverage"] == fixture["coverage"]
    assert record["current_execution_commit"] == fixture["current_commit"]
    assert record["previous_execution_commit"] == fixture["parent_commit"]
    assert len(record["repository_identity_chain"]) == 3
    assert manifest["repository"]["identity_history"] == record[
        "repository_identity_chain"
    ]
    registry = (run / "TRIAL_REGISTRY.jsonl").read_bytes()
    assert registry.startswith(fixture["parent_trials"])
    appended = [json.loads(line) for line in registry.splitlines()[-2:]]
    assert [row["trial_registration_id"] for row in appended] == list(
        repair.TRIAL_IDS
    )
    receipt_path = run / record["repair_receipt_path"]
    assert digest(receipt_path) == record["repair_receipt_sha256"]
    receipt_record = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert set(record) == set(receipt_record) | {
        "repair_receipt_path", "repair_receipt_sha256",
    }
    assert record["archive_inventory"] == repair.base.archive_inventory(
        run / record["archive_path"]
    )
    assert (run / record["session_resume_evidence_path"]).is_file()

    repeated = invoke(fixture)
    assert repeated["status"] == "REGISTRATION_REPAIR02_ALREADY_APPLIED"
    assert (run / "TRIAL_REGISTRY.jsonl").read_bytes() == registry


@pytest.mark.parametrize(
    "target_field",
    ["failed_input_identity_path", "structural_audit_path", "blocker_evidence_path"],
)
def test_archived_evidence_mutation_is_rejected(
    tmp_path, monkeypatch, target_field
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    invoke(fixture)
    manifest = json.loads(
        (fixture["run"] / "RUN_MANIFEST.json").read_text(encoding="utf-8")
    )
    record = manifest["data_integrity_repairs"][1]
    target = fixture["run"] / record[target_field]
    target.write_bytes(target.read_bytes() + b"tamper")
    with pytest.raises(repair.Repair02RegistrationError, match="archive|binding"):
        invoke(fixture)


def test_trial_registry_suffix_mutation_is_rejected(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    invoke(fixture)
    registry = fixture["run"] / "TRIAL_REGISTRY.jsonl"
    registry.write_bytes(registry.read_bytes() + b'{"unexpected":true}\n')
    with pytest.raises(repair.Repair02RegistrationError, match="trial"):
        invoke(fixture)


def test_authorization_semantic_mutation_is_rejected(tmp_path):
    run = tmp_path / "run"
    path = run / "DATA_INTEGRITY/REPAIR_02_USER_AUTHORIZATION.json"
    write_json(path, {
        "schema_version": repair.AUTHORIZATION_SCHEMA,
        "run_id": run.name,
        "repair_id": "repair-02",
        "authorized_at_utc": "2026-07-15T14:40:39Z",
        "authority_source": "operator_chat_message",
        "authorized_action": (
            f"批准 repair-02 整对象隔离 {repair.SECOND_KEY}; 禁止逐行修补; "
            "以 282/284、59,185,856,724 PARTIAL_OBJECT_COVERAGE_QUARANTINED "
            "从新 scratch 重跑 RFQ"
        ),
    })
    value = json.loads(path.read_text(encoding="utf-8"))
    value["authorized_action"] = value["authorized_action"].replace(
        "禁止逐行修补", "允许逐行修补"
    )
    write_json(path, value)
    with pytest.raises(repair.Repair02RegistrationError, match="authorization"):
        repair.validate_authorization(run, path)
