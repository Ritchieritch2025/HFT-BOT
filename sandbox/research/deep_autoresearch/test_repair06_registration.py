import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


HERE = Path(__file__).parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


repair = _load("repair06_registration_tested", HERE / "repair06_registration.py")
parent_tests = _load("repair06_parent_tests", HERE / "test_repair05_registration.py")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(repair.base.json_payload(value))


def snapshot(run: Path) -> dict[str, bytes]:
    return {
        path.relative_to(run).as_posix(): path.read_bytes()
        for path in sorted(run.rglob("*"))
        if path.is_file() and path.relative_to(run) != repair.repair03.RUN_LOCK
    }


def make_fixture(tmp_path: Path, monkeypatch) -> dict:
    fixture = parent_tests.make_transaction_fixture(tmp_path, monkeypatch)
    assert parent_tests.invoke(fixture)["status"] == "REGISTRATION_REPAIR05_REFROZEN"
    run, source = fixture["run"], fixture["source"]
    manifest = json.loads((run / "RUN_MANIFEST.json").read_text())
    parent = manifest["data_integrity_repairs"][-1]
    parent_commit = manifest["repository"]["execution_commit"]

    monkeypatch.setattr(repair, "PARENT_EXECUTION_COMMIT", parent_commit)
    monkeypatch.setattr(repair.repair04, "PARENT_EXECUTION_COMMIT", parent_commit)
    monkeypatch.setattr(repair, "PARENT_MANIFEST_SHA256", digest(run / "RUN_MANIFEST.json"))
    monkeypatch.setattr(repair, "PARENT_RECEIPT_SHA256", parent["repair_receipt_sha256"])
    monkeypatch.setattr(repair, "PARENT_JOURNAL_SHA256", parent["transaction_journal_sha256"])
    monkeypatch.setattr(repair, "PARENT_WIRING_SHA256", parent["wiring_contract_sha256"])
    monkeypatch.setattr(repair, "PARENT_AUTHORITY_SHA256", parent["authority_basis_sha256"])
    monkeypatch.setattr(repair, "W09_SHA256", digest(run / repair.W09_ATTESTATION))
    monkeypatch.setattr(repair, "UNCHANGED_STATE_SHA256", digest(run / repair.STATE))
    monkeypatch.setattr(repair, "UNCHANGED_INPUT_SHA256", digest(run / repair.INPUT_IDENTITY))
    monkeypatch.setattr(
        repair, "PARENT_QUERY_SHA256", digest(run / "queries/rfq_full_stage.py")
    )
    monkeypatch.setattr(
        repair.base,
        "validate_cycle1_duckdb_binding",
        lambda *_: ({"synthetic": True}, copy.deepcopy(parent["cycle1_duckdb_binding"])),
    )

    resource = repair._expected_resource(run.name)
    write_json(run / repair.FAILED_RESOURCE, resource)
    monkeypatch.setattr(repair, "FAILED_RESOURCE_SHA256", digest(run / repair.FAILED_RESOURCE))
    blocker = repair._expected_blocker(run.name)
    write_json(run / repair.BLOCKER, blocker)
    monkeypatch.setattr(repair, "BLOCKER_SHA256", digest(run / repair.BLOCKER))

    changes = {
        "finalize_mission.py": (
            "VERSION = 6\nCONSUMER_WIRING = True\nSTATUS_WIRING = True\n"
        ),
        "repair06_registration.py": "REPAIR = 6\n",
        "rfq_full_stage.py": (
            "VERSION = 6\nCONSUMER_WIRING = True\nSTATUS_WIRING = True\n"
        ),
        "test_finalize_mission.py": "TEST_REPAIR = 6\n",
        "test_repair06_registration.py": "TEST_REPAIR = 6\n",
        "test_rfq_full_stage.py": "TEST_REPAIR = 6\n",
    }
    for name, payload in changes.items():
        (source / name).write_text(payload, encoding="utf-8")
    current_commit = parent_tests.parent_tests.parent_tests.commit(
        fixture["repo"], "repair-06 source"
    )
    parent_rfq = repair.repair04._provenance_git_blob(
        fixture["repo"], parent_commit,
        f"{repair.SOURCE_RELATIVE}/rfq_full_stage.py",
    )
    current_rfq = (source / "rfq_full_stage.py").read_bytes()
    parent_order, parent_nodes = repair.repair05._top_level_ast_map(parent_rfq)
    current_order, current_nodes = repair.repair05._top_level_ast_map(current_rfq)
    additions = set(current_nodes) - set(parent_nodes)
    monkeypatch.setattr(
        repair, "PARENT_RFQ_MODULE_AST_SHA256",
        repair.repair05._ast_module_sha256(parent_rfq),
    )
    monkeypatch.setattr(
        repair, "APPROVED_REPAIR06_RFQ_MODULE_AST_SHA256",
        repair.repair05._ast_module_sha256(current_rfq),
    )
    monkeypatch.setattr(repair, "APPROVED_RFQ_AST_ADDITIONS", additions)
    monkeypatch.setattr(
        repair, "APPROVED_RFQ_AST_MODIFICATIONS",
        {
            key for key in set(parent_nodes) & set(current_nodes)
            if parent_nodes[key] != current_nodes[key]
        },
    )
    assert not (set(parent_nodes) - set(current_nodes))
    assert [key for key in current_order if key not in additions] == parent_order
    parent_finalizer = repair.repair04._provenance_git_blob(
        fixture["repo"], parent_commit,
        f"{repair.SOURCE_RELATIVE}/finalize_mission.py",
    )
    current_finalizer = (source / "finalize_mission.py").read_bytes()
    parent_order, parent_nodes = repair.repair05._top_level_ast_map(
        parent_finalizer
    )
    current_order, current_nodes = repair.repair05._top_level_ast_map(
        current_finalizer
    )
    additions = set(current_nodes) - set(parent_nodes)
    monkeypatch.setattr(
        repair, "PARENT_FINALIZER_MODULE_AST_SHA256",
        repair.repair05._ast_module_sha256(parent_finalizer),
    )
    monkeypatch.setattr(
        repair, "APPROVED_REPAIR06_FINALIZER_MODULE_AST_SHA256",
        repair.repair05._ast_module_sha256(current_finalizer),
    )
    monkeypatch.setattr(repair, "APPROVED_FINALIZER_AST_ADDITIONS", additions)
    monkeypatch.setattr(
        repair, "APPROVED_FINALIZER_AST_MODIFICATIONS",
        {
            key for key in set(parent_nodes) & set(current_nodes)
            if parent_nodes[key] != current_nodes[key]
        },
    )
    assert not (set(parent_nodes) - set(current_nodes))
    assert [key for key in current_order if key not in additions] == parent_order
    monkeypatch.setattr(repair, "_run_main_preflight", lambda *_: None)
    return {
        **fixture,
        "parent": parent,
        "parent_commit": parent_commit,
        "current_commit": current_commit,
        "resource": resource,
        "blocker": blocker,
    }


def invoke(fixture: dict):
    return repair.repair06_registration(fixture["run"], fixture["source"])


def test_canonical_contract_authority_and_evidence_shapes():
    run_id = "20260715T112538Z__c21a79a8cff__deep01"
    resource = repair._expected_resource(run_id)
    blocker = repair._expected_blocker(run_id)
    assert resource["command"] == repair.expected_command(run_id)
    assert resource["wall_seconds"] == 0.114
    assert blocker["error"] == repair.ERROR
    assert blocker["failure_phase"] == repair.FAILURE_PHASE
    parent = {
        "repair_receipt_path": "DATA_INTEGRITY/repairs/repair-05/REPAIR_REGISTRATION.json",
        "transaction_journal_path": "DATA_INTEGRITY/repairs/repair-05/TRANSACTION_JOURNAL.json",
        "wiring_contract_path": "DATA_INTEGRITY/repairs/repair-05/RFQ_CONSUMER_VALIDATION_WIRING_CONTRACT.json",
        "authority_basis_path": "DATA_INTEGRITY/repairs/repair-05/AUTHORITY_BASIS.json",
        "core_result_artifacts": [], "coverage": {}, "cycle1_duckdb_binding": {},
        "parser_contract": {}, "resource_contract": {},
    }
    contract = repair.make_status_wiring_contract(
        run_id, "2026-07-15T20:00:00Z", parent
    )
    assert contract["schema_version"] == repair.CONTRACT_SCHEMA
    assert contract["preflight"]["overlay_path"] == repair.POST_ROOT
    assert contract["preflight"]["stop_before_input_discovery"] is True
    assert contract["preflight"]["stop_before_repair_replay"] is True
    assert contract["retry"]["expected_success_resource"] == repair.SUCCESS_RESOURCE
    authority = repair.make_authority(
        run_id, "2026-07-15T20:00:00Z", f"{repair.PREFIX}/{repair.CONTRACT_FILE}", "a" * 64
    )
    assert authority["permitted_change"] == repair.CHANGE_CLASS


def test_success_is_append_only_lightweight_and_idempotent(tmp_path, monkeypatch):
    fixture = make_fixture(tmp_path, monkeypatch)
    before = (fixture["run"] / "TRIAL_REGISTRY.jsonl").read_bytes()
    result = invoke(fixture)
    assert result["status"] == "REGISTRATION_REPAIR06_REFROZEN"
    manifest = json.loads((fixture["run"] / "RUN_MANIFEST.json").read_text())
    record = manifest["data_integrity_repairs"][-1]
    after = (fixture["run"] / "TRIAL_REGISTRY.jsonl").read_bytes()
    assert after.startswith(before)
    assert len(repair.repair05._read_registry_rows(after[len(before):])) == 2
    assert record["schema_version"] == repair.REPAIR_SCHEMA
    assert record["expected_success_resource"] == repair.SUCCESS_RESOURCE
    assert record["preserved_scratch_policy"] == repair.PRESERVED_SCRATCH_POLICY
    assert all("scratch" not in row["path"].lower() for row in record["archive_inventory"])
    assert record["archive_inventory"] == repair.base.archive_inventory(
        fixture["run"] / repair.PRE_ROOT
    )
    again = invoke(fixture)
    assert again["status"] == "REGISTRATION_REPAIR06_ALREADY_REFROZEN"


@pytest.mark.parametrize(
    "boundary",
    [
        "publish_repair_dir",
        "main_preflight",
        "write:SOURCE_MANIFEST.json",
        "write:TRIAL_REGISTRY.jsonl",
        "write:RUN_MANIFEST.json",
    ],
)
def test_minimal_transaction_boundaries_restore_parent(
    tmp_path, monkeypatch, boundary
):
    fixture = make_fixture(tmp_path, monkeypatch)
    before = snapshot(fixture["run"])

    def fault(label: str) -> None:
        if label == boundary:
            raise RuntimeError(boundary)

    monkeypatch.setattr(repair, "_transaction_fault_hook", fault)
    with pytest.raises(RuntimeError, match=boundary):
        invoke(fixture)
    assert snapshot(fixture["run"]) == before
    assert not (fixture["run"] / repair.PREFIX).exists()


def test_preflight_invokes_real_main_overlay_interface(tmp_path, monkeypatch):
    run = tmp_path / "run-id"
    source = tmp_path / "source"
    overlay = run / repair.POST_ROOT
    source.mkdir(parents=True)
    (source / "rfq_full_stage.py").write_text("pass\n", encoding="utf-8")
    captured = {}

    class Result:
        returncode = 0
        stdout = f"RFQ_VALIDATE_RUN_PREFLIGHT_COMPLETE run_id={run.name}\n"
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Result()

    monkeypatch.setattr(repair.subprocess, "run", fake_run)
    repair._run_main_preflight(run, source, overlay)
    assert captured["command"][-3:] == [
        "--validate-run-preflight-only",
        "--validate-run-preflight-overlay",
        str(overlay),
    ]
    assert captured["command"] == [
        sys.executable,
        str(source / "rfq_full_stage.py"),
        "--run-dir", str(run),
        "--validate-run-preflight-only",
        "--validate-run-preflight-overlay", str(overlay),
    ]


def test_gitless_cli_legacy_authority_is_not_accepted(tmp_path):
    with pytest.raises(repair.Repair06RegistrationError):
        repair.repair06_registration(tmp_path / "run", tmp_path / "source")


def test_cli_help_uses_source_loaded_parent_only():
    result = subprocess.run(
        [sys.executable, str(HERE / "repair06_registration.py"), "--help"],
        text=True, capture_output=True,
    )
    assert result.returncode == 0
    assert "--run-dir" in result.stdout
