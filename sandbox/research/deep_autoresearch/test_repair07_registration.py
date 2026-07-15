import copy
import hashlib
import importlib.util
import json
import os
import stat
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


repair = _load("repair07_registration_tested", HERE / "repair07_registration.py")
parent_tests = _load(
    "repair07_parent_tests", HERE / "test_repair06_registration.py"
)


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
    fixture = parent_tests.make_fixture(tmp_path, monkeypatch)
    assert parent_tests.invoke(fixture)["status"] == "REGISTRATION_REPAIR06_REFROZEN"
    run, source = fixture["run"], fixture["source"]
    manifest = json.loads((run / "RUN_MANIFEST.json").read_text())
    parent = manifest["data_integrity_repairs"][-1]
    parent_commit = manifest["repository"]["execution_commit"]

    monkeypatch.setattr(repair, "PARENT_EXECUTION_COMMIT", parent_commit)
    monkeypatch.setattr(repair.repair04, "PARENT_EXECUTION_COMMIT", parent_commit)
    monkeypatch.setattr(repair, "PARENT_MANIFEST_SHA256", digest(run / "RUN_MANIFEST.json"))
    monkeypatch.setattr(repair, "PARENT_RECEIPT_SHA256", parent["repair_receipt_sha256"])
    monkeypatch.setattr(repair, "PARENT_JOURNAL_SHA256", parent["transaction_journal_sha256"])
    monkeypatch.setattr(
        repair, "PARENT_STATUS_CONTRACT_SHA256",
        parent["status_wiring_contract_sha256"],
    )
    monkeypatch.setattr(repair, "PARENT_AUTHORITY_SHA256", parent["authority_basis_sha256"])
    monkeypatch.setattr(
        repair, "PARENT_RESOURCE_CONTRACT_SHA256", parent["resource_contract_sha256"]
    )
    monkeypatch.setattr(
        repair, "PARENT_QUERY_SHA256", parent["registered_rfq_query_sha256"]
    )
    monkeypatch.setattr(
        repair.base,
        "validate_cycle1_duckdb_binding",
        lambda *_: ({"synthetic": True}, copy.deepcopy(parent["cycle1_duckdb_binding"])),
    )

    resource = {
        "schema_version": "w09-stage-resource-v1",
        "label": "rfq_full_stage_repair06",
        "command": [
            value if value not in {"20GB", "80"} else {"20GB": "70GB", "80": "100"}[value]
            for value in repair.expected_command(run.name)
        ],
        "started_at_utc": "2026-07-15T20:22:56.347028Z",
        "completed_at_utc": "2026-07-15T20:38:50.636028Z",
        "wall_seconds": 954.289,
        "return_code": 1,
        "cpu_user_seconds": 1819.425,
        "cpu_system_seconds": 59.105,
        "peak_process_tree_rss_kib_polled": 46_022_424,
        "peak_temp_bytes_polled": 11_550_019_656,
        "minimum_disk_free_bytes_polled": 83_262_455_808,
        "disk_free_before_bytes": 123_563_278_336,
        "disk_free_after_bytes": 94_802_587_648,
    }
    write_json(run / repair.RESOURCE, resource)
    monkeypatch.setattr(repair, "FAILED_RESOURCE_SHA256", digest(run / repair.RESOURCE))
    state = {
        "status": "FAILED_DATA_INTEGRITY_REQUIRES_NEW_PREREGISTRATION",
        "error_type": repair.ERROR_TYPE,
        "error": "Out of Memory Error: could not allocate block of size 256.0 KiB",
        "expected_success_resource": {
            "label": "rfq_full_stage_repair06",
            "path": repair.RESOURCE.as_posix(),
        },
        "registered_rfq_query_sha256": parent["registered_rfq_query_sha256"],
        "input_fingerprint": (
            "8b310c37f3989770d1f53a058e5ef396e1eb5c9c3d24d07ac87bd9d8aee5b9e1"
        ),
    }
    write_json(run / repair.STATE, state)
    monkeypatch.setattr(repair, "FAILED_STATE_SHA256", digest(run / repair.STATE))
    identity = {
        "schema": "rfq-full-input-identity-v6",
        "registered_rfq_query_sha256": parent["registered_rfq_query_sha256"],
        "consumed_unique_objects": 282,
        "consumed_bytes": 59_185_856_724,
    }
    write_json(run / repair.INPUT_IDENTITY, identity)
    monkeypatch.setattr(repair, "FAILED_INPUT_SHA256", digest(run / repair.INPUT_IDENTITY))

    scratch = run / repair.SCRATCH
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_bytes(b"synthetic failed DuckDB scratch")
    scratch_mtime_ns = 1_700_000_000_123_456_789
    os.utime(scratch, ns=(scratch_mtime_ns, scratch_mtime_ns))
    monkeypatch.setattr(repair, "FAILED_SCRATCH_BYTES", scratch.stat().st_size)
    monkeypatch.setattr(repair, "FAILED_SCRATCH_MTIME_NS", scratch_mtime_ns)
    monkeypatch.setattr(repair, "FAILED_SCRATCH_SHA256", digest(scratch))
    monkeypatch.setattr(
        repair, "_scratch_bucket_rows", lambda *_: copy.deepcopy(repair.BUCKET_COUNTS)
    )

    changes = {
        "finalize_mission.py": "VERSION = 7\nFINALIZER_REPAIR07 = True\n",
        "repair07_registration.py": "REPAIR = 7\n",
        "rfq_full_stage.py": "VERSION = 7\nBUCKETED_DEDUP = True\n",
        "test_finalize_mission.py": "TEST_REPAIR = 7\n",
        "test_repair07_registration.py": "TEST_REPAIR = 7\n",
        "test_rfq_full_stage.py": "TEST_REPAIR = 7\n",
    }
    for name, payload in changes.items():
        (source / name).write_text(payload, encoding="utf-8")
    current_commit = parent_tests.parent_tests.parent_tests.parent_tests.commit(
        fixture["repo"], "repair-07 source"
    )
    monkeypatch.setattr(repair, "_run_main_preflight", lambda *_: None)
    return {
        **fixture,
        "parent": parent,
        "parent_commit": parent_commit,
        "current_commit": current_commit,
        "resource": resource,
        "state": state,
        "identity": identity,
        "scratch_payload": b"synthetic failed DuckDB scratch",
    }


def invoke(fixture: dict):
    return repair.repair07_registration(fixture["run"], fixture["source"])


def test_frozen_bucket_inventory_and_contract_shape():
    repair._validate_bucket_constants()
    assert len(repair.BUCKET_COUNTS) == 44
    assert sum(row["valid_contract_rows"] for row in repair.BUCKET_COUNTS) == 58_142_605
    assert max(row["valid_contract_rows"] for row in repair.BUCKET_COUNTS) == 2_982_434
    receipt = repair.make_scratch_receipt("run", "2026-07-15T21:00:00Z")
    blocker = repair.make_blocker(
        "run", "2026-07-15T21:00:00Z", "a" * 64,
        {"error": "Out of Memory Error"},
    )
    contract = repair.make_resource_contract(
        "run", "2026-07-15T21:00:00Z", "a" * 64, "b" * 64
    )
    assert receipt["preservation"] == "ATOMIC_RENAME_SAME_FILESYSTEM_NO_COPY_NO_DELETE"
    assert blocker["finding"] == repair.FINDING
    assert contract["current_runtime"] == repair.RUNTIME
    assert contract["dedup_equivalence"]["bucket_counts"] == repair.BUCKET_COUNTS
    assert contract["dedup_equivalence"]["query_semantics_change"] == "NONE"
    assert contract["expected_command"] == repair.expected_command("run")


def test_success_preserves_scratch_and_is_lightweight_idempotent(tmp_path, monkeypatch):
    fixture = make_fixture(tmp_path, monkeypatch)
    run = fixture["run"]
    before_registry = (run / "TRIAL_REGISTRY.jsonl").read_bytes()
    result = invoke(fixture)
    assert result["status"] == "REGISTRATION_REPAIR07_REFROZEN"
    assert not (run / repair.SCRATCH).exists()
    assert (run / repair.PRESERVED_SCRATCH).read_bytes() == fixture["scratch_payload"]
    manifest = json.loads((run / "RUN_MANIFEST.json").read_text())
    record = manifest["data_integrity_repairs"][-1]
    assert record["schema_version"] == repair.REPAIR_SCHEMA
    assert record["expected_success_resource"] == repair.SUCCESS_RESOURCE
    assert record["resource_contract"]["current_runtime"] == repair.RUNTIME
    assert record["preserved_scratch_policy"] == repair.PRESERVED_SCRATCH_POLICY
    assert all("scratch" not in row["path"].lower() for row in record["archive_inventory"])
    after_registry = (run / "TRIAL_REGISTRY.jsonl").read_bytes()
    assert after_registry.startswith(before_registry)
    assert len(repair.repair05._read_registry_rows(after_registry[len(before_registry):])) == 2
    assert invoke(fixture)["status"] == "REGISTRATION_REPAIR07_ALREADY_REFROZEN"


@pytest.mark.parametrize(
    "boundary",
    [
        "publish_repair_dir",
        "preserve_failed_scratch",
        "main_preflight",
        "write:SOURCE_MANIFEST.json",
        "write:TRIAL_REGISTRY.jsonl",
        "write:RUN_MANIFEST.json",
    ],
)
def test_transaction_fault_restores_parent_and_scratch(tmp_path, monkeypatch, boundary):
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
    assert (fixture["run"] / repair.SCRATCH).is_file()
    assert not (fixture["run"] / repair.PRESERVED_SCRATCH).exists()


def test_post_preflight_preserved_scratch_is_read_only_and_rolls_back(
    tmp_path, monkeypatch
):
    fixture = make_fixture(tmp_path, monkeypatch)
    run = fixture["run"]
    before = snapshot(run)
    observed_modes = []

    def inspect_after_preflight(label: str) -> None:
        if label == "main_preflight":
            mode = stat.S_IMODE(os.lstat(run / repair.PRESERVED_SCRATCH).st_mode)
            observed_modes.append(mode)
            raise RuntimeError("verified read-only preserved scratch")

    monkeypatch.setattr(repair, "_transaction_fault_hook", inspect_after_preflight)
    with pytest.raises(RuntimeError, match="verified read-only preserved scratch"):
        invoke(fixture)
    assert len(observed_modes) == 1
    assert observed_modes[0] & 0o222 == 0
    assert snapshot(run) == before
    assert (run / repair.SCRATCH).read_bytes() == fixture["scratch_payload"]
    assert digest(run / repair.SCRATCH) == repair.FAILED_SCRATCH_SHA256
    assert not (run / repair.PRESERVED_SCRATCH).exists()
    assert not (run / repair.PREFIX).exists()


def test_post_preflight_same_size_mtime_restored_mutation_fails_closed(
    tmp_path, monkeypatch
):
    fixture = make_fixture(tmp_path, monkeypatch)
    run = fixture["run"]
    parent_manifest = (run / "RUN_MANIFEST.json").read_bytes()
    changed = b"Y" * len(fixture["scratch_payload"])
    assert changed != fixture["scratch_payload"]

    def force_mutation_after_preflight(label: str) -> None:
        if label != "main_preflight":
            return
        preserved = run / repair.PRESERVED_SCRATCH
        os.chmod(preserved, 0o600)
        preserved.write_bytes(changed)
        os.utime(
            preserved,
            ns=(repair.FAILED_SCRATCH_MTIME_NS, repair.FAILED_SCRATCH_MTIME_NS),
        )

    monkeypatch.setattr(
        repair, "_transaction_fault_hook", force_mutation_after_preflight
    )
    with pytest.raises(
        repair.Repair07RegistrationError,
        match="repair-07 rollback blocked: .*preserved scratch changed before recovery",
    ):
        invoke(fixture)

    # The first full post-preflight hash rejects a same-size mutation even when
    # its mtime is restored. The damaged object is never promoted back to the
    # active name and the parent manifest is never refrozen as repair-07.
    assert (run / "RUN_MANIFEST.json").read_bytes() == parent_manifest
    assert not (run / repair.SCRATCH).exists()
    assert (run / repair.PRESERVED_SCRATCH).read_bytes() == changed
    assert digest(run / repair.PRESERVED_SCRATCH) != repair.FAILED_SCRATCH_SHA256
    assert (run / repair.PREFIX).is_dir()


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
        "--validate-run-preflight-overlay", str(overlay),
    ]


def test_cli_help():
    result = subprocess.run(
        [sys.executable, str(HERE / "repair07_registration.py"), "--help"],
        text=True, capture_output=True,
    )
    assert result.returncode == 0
    assert "--run-dir" in result.stdout
