import ast
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).with_name("repair05_registration.py")
SPEC = importlib.util.spec_from_file_location("repair05_registration", MODULE_PATH)
repair = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(repair)

PARENT_TEST_PATH = Path(__file__).with_name("test_repair04_registration.py")
PARENT_SPEC = importlib.util.spec_from_file_location(
    "repair05_parent_fixture", PARENT_TEST_PATH
)
parent_tests = importlib.util.module_from_spec(PARENT_SPEC)
assert PARENT_SPEC.loader is not None
PARENT_SPEC.loader.exec_module(parent_tests)


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(repair.base.json_payload(value))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(run: Path) -> dict[str, bytes]:
    return {
        path.relative_to(run).as_posix(): path.read_bytes()
        for path in sorted(run.rglob("*"))
        if path.is_file()
        and path.relative_to(run) != repair.repair03.RUN_LOCK
    }


def _scratch_identity(path: Path) -> tuple[int, int, int, int, int]:
    value = path.stat()
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def make_transaction_fixture(tmp_path: Path, monkeypatch) -> dict:
    fixture = parent_tests.make_transaction_fixture(tmp_path, monkeypatch)
    assert parent_tests.invoke(fixture)["status"] == "REGISTRATION_REPAIR04_REFROZEN"
    run = fixture["run"]
    source = fixture["source"]
    manifest = json.loads((run / "RUN_MANIFEST.json").read_text())
    parent = manifest["data_integrity_repairs"][-1]
    parent_commit = manifest["repository"]["execution_commit"]
    monkeypatch.setattr(repair, "PARENT_EXECUTION_COMMIT", parent_commit)
    monkeypatch.setattr(repair.repair04, "PARENT_EXECUTION_COMMIT", parent_commit)

    resource = repair._expected_failed_resource(run.name)
    blocker = repair._expected_blocker(run.name)
    write_json(run / repair.RESOURCE, resource)
    write_json(run / repair.BLOCKER, blocker)
    monkeypatch.setattr(repair, "FAILED_RESOURCE_SHA256", digest(run / repair.RESOURCE))
    monkeypatch.setattr(repair, "BLOCKER_SHA256", digest(run / repair.BLOCKER))
    monkeypatch.setattr(repair, "UNCHANGED_STATE_SHA256", digest(run / repair.STATE))
    monkeypatch.setattr(
        repair, "UNCHANGED_INPUT_SHA256", digest(run / repair.INPUT_IDENTITY)
    )

    changed = {
        "finalize_mission.py": "VERSION = 5\nCONSUMER_WIRING = True\n",
        "repair05_registration.py": "REPAIR = 5\n",
        "rfq_full_stage.py": "VERSION = 5\nCONSUMER_WIRING = True\n",
        "test_finalize_mission.py": "TEST_REPAIR = 5\n",
        "test_repair05_registration.py": "TEST_REPAIR = 5\n",
        "test_rfq_full_stage.py": "TEST_REPAIR = 5\n",
    }
    for name, payload in changed.items():
        (source / name).write_text(payload, encoding="utf-8")
    current_commit = parent_tests.parent_tests.commit(
        fixture["repo"], "repair-05 source"
    )
    changed_paths = parent_tests.parent_tests.command(
        fixture["repo"],
        "git",
        "diff",
        "--no-renames",
        "--name-only",
        f"{parent_commit}..{current_commit}",
    ).splitlines()
    assert changed_paths == repair.EXPECTED_REPAIR05_CHANGED_PATHS

    registry = (run / "TRIAL_REGISTRY.jsonl").read_bytes()
    rows = repair._read_registry_rows(registry)
    core = repair.base.core_result_inventory(run)
    cycle = copy.deepcopy(parent["cycle1_duckdb_binding"])
    old_repository = copy.deepcopy(manifest["repository"])
    monkeypatch.setattr(
        repair,
        "validate_parent_repair",
        lambda *_: (
            copy.deepcopy(parent),
            copy.deepcopy(old_repository),
            registry,
            copy.deepcopy(rows),
            copy.deepcopy(core),
            copy.deepcopy(cycle),
            {"sha256": digest(run / repair.W09_ATTESTATION)},
        ),
    )
    monkeypatch.setattr(
        repair.base,
        "validate_cycle1_duckdb_binding",
        lambda *_: ({"synthetic": True}, copy.deepcopy(cycle)),
    )
    monkeypatch.setattr(
        repair.repair04,
        "validate_w09_attestation",
        lambda *_: (
            {"synthetic": True}, digest(run / repair.W09_ATTESTATION)
        ),
    )
    evidence = {
        "run_id": run.name,
        "resource": copy.deepcopy(resource),
        "blocker": copy.deepcopy(blocker),
        "failed_resource_receipt_path": (
            "DATA_INTEGRITY/repairs/repair-05/pre_repair/"
            f"{repair.RESOURCE.as_posix()}"
        ),
        "failed_resource_receipt_sha256": digest(run / repair.RESOURCE),
        "blocker_path": (
            "DATA_INTEGRITY/repairs/repair-05/pre_repair/"
            f"{repair.BLOCKER.as_posix()}"
        ),
        "blocker_sha256": digest(run / repair.BLOCKER),
        "unchanged_state_path": (
            "DATA_INTEGRITY/repairs/repair-05/pre_repair/"
            f"{repair.STATE.as_posix()}"
        ),
        "unchanged_state_sha256": digest(run / repair.STATE),
        "unchanged_input_identity_path": (
            "DATA_INTEGRITY/repairs/repair-05/pre_repair/"
            f"{repair.INPUT_IDENTITY.as_posix()}"
        ),
        "unchanged_input_identity_sha256": digest(run / repair.INPUT_IDENTITY),
    }
    monkeypatch.setattr(
        repair, "validate_failed_attempt", lambda *_: copy.deepcopy(evidence)
    )
    preserved = fixture["attempt04_preserved"]
    preserved_payload = preserved.read_bytes()

    def capture_preserved(*_):
        if preserved.read_bytes() != preserved_payload:
            repair.fail("synthetic preserved scratch changed")
        return {preserved: _scratch_identity(preserved)}

    monkeypatch.setattr(
        repair.repair04,
        "_capture_preserved_scratch_identities",
        capture_preserved,
    )

    def source_provenance(*_):
        _, source_manifest, source_sums = repair.base.build_source_receipts(
            source, fixture["repo"]
        )
        query_rows = []
        for relative in old_repository["query_files"]:
            path = source / Path(relative).name
            payload = path.read_bytes()
            query_rows.append((
                repair.base.sha256_bytes(payload), relative, payload
            ))
        return (
            fixture["repo"],
            current_commit,
            source_manifest,
            source_sums,
            query_rows,
        )

    monkeypatch.setattr(repair, "validate_source_provenance", source_provenance)
    monkeypatch.setattr(repair.base, "now_utc", lambda: "2026-07-15T18:45:00Z")
    return {
        **fixture,
        "parent": parent,
        "parent_commit": parent_commit,
        "current_commit": current_commit,
        "resource": resource,
        "blocker": blocker,
    }


def invoke(fixture: dict):
    return repair.repair05_registration(fixture["run"], fixture["source"])


def test_exact_evidence_and_contract_shapes():
    run_id = "20260715T112538Z__c21a79a8cff__deep01"
    resource = repair._expected_failed_resource(run_id)
    blocker = repair._expected_blocker(run_id)
    assert resource["return_code"] == 1
    assert resource["command"] == repair.expected_retry_command(run_id)
    assert resource["peak_process_tree_rss_kib_polled"] == 35_988
    assert resource["minimum_disk_free_bytes_polled"] == 124_105_936_896
    assert blocker["error"] == "KeyError: 'cycle1_duckdb_binding'"
    assert blocker["registered_query_sha256"] == repair.PARENT_RFQ_QUERY_SHA256
    evidence = {
        "blocker_path": "DATA_INTEGRITY/repairs/repair-05/pre_repair/" + repair.BLOCKER.as_posix(),
        "blocker_sha256": repair.BLOCKER_SHA256,
        "failed_resource_receipt_path": "DATA_INTEGRITY/repairs/repair-05/pre_repair/" + repair.RESOURCE.as_posix(),
        "failed_resource_receipt_sha256": repair.FAILED_RESOURCE_SHA256,
        "cycle1_binding_path": "DATA_INTEGRITY/repairs/repair-05/pre_repair/" + repair.CYCLE1_BINDING.as_posix(),
        "cycle1_binding_sha256": "a" * 64,
        "registered_cycle1_duckdb_binding": {"active_sha256": "a" * 64},
    }
    contract = repair.make_wiring_contract(run_id, "2026-07-15T18:45:00Z", evidence, "b" * 64)
    assert contract["cycle1_binding"] == {
        "active_path": repair.CYCLE1_BINDING.as_posix(),
        "active_sha256": "a" * 64,
        "archived_path": evidence["cycle1_binding_path"],
        "archived_sha256": "a" * 64,
        "registered_cycle1_duckdb_binding": {"active_sha256": "a" * 64},
    }
    assert contract["inherited_contracts"]["runtime"] == repair.RUNTIME_CONTRACT
    assert contract["resource_contract_change"] == "NONE"


def test_sql_call_fingerprint_allows_wiring_only_and_rejects_sql_change():
    parent = b"def run(c):\n    return c.execute('SELECT 1')\n"
    wiring_only = b"FLAG = True\n\ndef run(c):\n    return c.execute('SELECT 1')\n"
    changed_sql = b"def run(c):\n    return c.execute('SELECT 2')\n"
    assert repair._sql_call_fingerprint(parent) == repair._sql_call_fingerprint(
        wiring_only
    )
    assert repair._sql_call_fingerprint(parent) != repair._sql_call_fingerprint(
        changed_sql
    )


def _set_simulated_type_params(tree, present: bool, value=None) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            continue
        fields = tuple(
            field for field in node._fields if field != "type_params"
        )
        if present:
            node._fields = (*fields, "type_params")
            node.type_params = [] if value is None else value
        else:
            node._fields = fields
            if "type_params" in getattr(node, "__dict__", {}):
                del node.type_params


def test_canonical_ast_hash_is_identical_for_simulated_python39_and_312():
    source = (
        b"@decorator\n"
        b"class Box(Base):\n"
        b"    def value(self, x: int = 1) -> int:\n"
        b"        return x + 2\n"
    )
    python39 = ast.parse(source)
    python312 = ast.parse(source)
    _set_simulated_type_params(python39, False)
    _set_simulated_type_params(python312, True)
    digest39 = repair._canonical_ast_sha256(python39)
    digest312 = repair._canonical_ast_sha256(python312)
    assert digest39 == digest312
    assert digest39 == (
        "bef1f0ce35d7fe498590bc8d9e3f6da1967d2bb0c972816c910c0437c1b00d6d"
    )


def test_canonical_ast_rejects_nonempty_type_params_and_unknown_fields():
    parameterized = ast.parse(b"def f():\n    return 1\n")
    _set_simulated_type_params(
        parameterized,
        True,
        [ast.Name(id="T", ctx=ast.Load())],
    )
    with pytest.raises(
        repair.Repair05RegistrationError,
        match="non-empty compatibility field FunctionDef.type_params",
    ):
        repair._canonical_ast_sha256(parameterized)

    extended = ast.parse(b"value = 1\n")
    assignment = extended.body[0]
    assignment._fields = (*assignment._fields, "future_semantics")
    assignment.future_semantics = ast.Constant(value=2)
    with pytest.raises(
        repair.Repair05RegistrationError,
        match="unknown semantic fields on Assign",
    ):
        repair._canonical_ast_sha256(extended)


@pytest.mark.parametrize(
    ("parent", "changed"),
    [
        (
            b"statements = {'q': 'SELECT 1'}\n"
            b"def run(c):\n    return c.execute(statements['q'])\n",
            b"statements = {'q': 'SELECT 2'}\n"
            b"def run(c):\n    return c.execute(statements['q'])\n",
        ),
        (
            b"def scan_sql():\n    return 'SELECT 1'\n"
            b"def run(c):\n    return c.execute(scan_sql())\n",
            b"def scan_sql():\n    return 'SELECT 2'\n"
            b"def run(c):\n    return c.execute(scan_sql())\n",
        ),
        (
            b"def indirect_query():\n    return 'SELECT 1'\n"
            b"def run(c):\n    q = indirect_query()\n    return c.execute(q)\n",
            b"def indirect_query():\n    return 'SELECT 2'\n"
            b"def run(c):\n    q = indirect_query()\n    return c.execute(q)\n",
        ),
    ],
)
def test_ast_allowlist_rejects_indirect_sql_changes_with_same_execute_ast(
    parent, changed
):
    assert repair._sql_call_fingerprint(parent) == repair._sql_call_fingerprint(
        changed
    )
    with pytest.raises(
        repair.Repair05RegistrationError, match="modification scope mismatch"
    ):
        repair._validate_consumer_governance_ast_delta(
            parent,
            changed,
            approved_additions=set(),
            approved_modifications=set(),
            expected_parent_ast_sha256=repair._ast_module_sha256(parent),
            expected_current_ast_sha256=repair._ast_module_sha256(changed),
        )


@pytest.mark.parametrize(
    ("parent", "changed"),
    [
        (b"PARSER_MARKERS = ('gap',)\n", b"PARSER_MARKERS = ('gap', 'data')\n"),
        (b"WINDOWS_US = (100_000,)\n", b"WINDOWS_US = (200_000,)\n"),
        (b"QUARANTINE_POLICY = 'WHOLE_OBJECT'\n", b"QUARANTINE_POLICY = 'LINE_SALVAGE'\n"),
        (
            b"def final_conclusion(x):\n    return x['registered']\n",
            b"def final_conclusion(x):\n    return True\n",
        ),
    ],
)
def test_ast_allowlist_rejects_parser_threshold_quarantine_and_conclusion_changes(
    parent, changed
):
    with pytest.raises(
        repair.Repair05RegistrationError, match="modification scope mismatch"
    ):
        repair._validate_consumer_governance_ast_delta(
            parent,
            changed,
            approved_additions=set(),
            approved_modifications=set(),
            expected_parent_ast_sha256=repair._ast_module_sha256(parent),
            expected_current_ast_sha256=repair._ast_module_sha256(changed),
        )


def test_success_is_append_only_refrozen_and_idempotent(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    before = (fixture["run"] / "TRIAL_REGISTRY.jsonl").read_bytes()
    result = invoke(fixture)
    assert result["status"] == "REGISTRATION_REPAIR05_REFROZEN"
    manifest = json.loads((fixture["run"] / "RUN_MANIFEST.json").read_text())
    record = manifest["data_integrity_repairs"][-1]
    after = (fixture["run"] / "TRIAL_REGISTRY.jsonl").read_bytes()
    assert after.startswith(before)
    assert len(repair._read_registry_rows(after[len(before):])) == 2
    assert record["schema_version"] == repair.REPAIR_SCHEMA
    assert record["cycle1_binding_path"].endswith(
        repair.CYCLE1_BINDING.as_posix()
    )
    assert record["trial_registry"]["trial_registration_ids"] == list(
        repair.TRIAL_IDS
    )
    assert record["resource_contract_sha256"] == fixture["parent"][
        "resource_contract_sha256"
    ]
    assert record["resource_contract_change"] == "NONE"
    assert [row["repair_id"] for row in manifest["data_integrity_repairs"]] == [
        "repair-01", "repair-02", "repair-03", "repair-04", "repair-05"
    ]
    idempotent = invoke(fixture)
    assert idempotent["status"] == "REGISTRATION_REPAIR05_ALREADY_REFROZEN"


@pytest.mark.parametrize(
    "boundary",
    [
        "publish_repair_dir",
        "write:SOURCE_MANIFEST.json",
        "write:queries/rfq_full_stage.py",
        "write:TRIAL_REGISTRY.jsonl",
        "write:RUN_MANIFEST.json",
    ],
)
def test_transaction_boundaries_restore_exact_parent(
    tmp_path, monkeypatch, boundary
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    before = snapshot(fixture["run"])

    def inject(label: str) -> None:
        if label == boundary:
            raise RuntimeError(f"fault:{boundary}")

    monkeypatch.setattr(repair, "_transaction_fault_hook", inject)
    with pytest.raises(RuntimeError, match="fault:"):
        invoke(fixture)
    assert snapshot(fixture["run"]) == before
    assert not (fixture["run"] / "DATA_INTEGRITY/repairs/repair-05").exists()


def test_rollback_preserves_foreign_registry_suffix(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    registry = fixture["run"] / "TRIAL_REGISTRY.jsonl"
    original = registry.read_bytes()
    foreign = repair.base.registry_line({
        "trial_registration_id": "FOREIGN_CONCURRENT_APPEND",
        "record_type": "FOREIGN",
    })

    def inject(label: str) -> None:
        if label == "write:TRIAL_REGISTRY.jsonl":
            with registry.open("ab") as handle:
                handle.write(foreign)
                handle.flush()
                os.fsync(handle.fileno())
            raise RuntimeError("foreign suffix")

    monkeypatch.setattr(repair, "_transaction_fault_hook", inject)
    with pytest.raises(RuntimeError, match="foreign suffix"):
        invoke(fixture)
    assert registry.read_bytes() == original + foreign
    assert not (fixture["run"] / "DATA_INTEGRITY/repairs/repair-05").exists()


def test_partial_registry_write_is_atomically_removed(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    registry = fixture["run"] / "TRIAL_REGISTRY.jsonl"
    original = registry.read_bytes()
    real_write = repair.os.write
    called = False

    def short_write(descriptor: int, payload: bytes) -> int:
        nonlocal called
        if not called and b"RFQ_FULL_STAGE_ATTEMPT_05" in payload:
            called = True
            return real_write(descriptor, payload[: max(1, len(payload) // 3)])
        return real_write(descriptor, payload)

    monkeypatch.setattr(repair.os, "write", short_write)
    with pytest.raises(repair.Repair05RegistrationError, match="partial"):
        invoke(fixture)
    assert called
    assert registry.read_bytes() == original
    assert not (fixture["run"] / "DATA_INTEGRITY/repairs/repair-05").exists()


def test_clean_git_scope_is_direct_child_and_exact_six(tmp_path, monkeypatch):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    repo_root, source_relative, commit = repair.repair04._verify_clean_source_provenance(
        fixture["source"]
    )
    assert source_relative == repair.SOURCE_RELATIVE
    assert commit == fixture["current_commit"]
    repair.repair04._validate_git_commit_scope(repo_root, commit)
    extra = fixture["source"] / "unapproved.py"
    extra.write_text("UNAPPROVED = True\n")
    parent_tests.parent_tests.commit(fixture["repo"], "unapproved source")
    with pytest.raises(repair.repair04.Repair04RegistrationError):
        repair.repair04._validate_git_commit_scope(
            fixture["repo"],
            parent_tests.parent_tests.command(
                fixture["repo"], "git", "rev-parse", "HEAD"
            ),
        )


def test_dirty_source_is_rejected_by_shared_strict_git_proof(
    tmp_path, monkeypatch
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    (fixture["source"] / "rfq_full_stage.py").write_text(
        "DIRTY = True\n", encoding="utf-8"
    )
    with pytest.raises(
        repair.repair04.Repair04RegistrationError, match="committed and clean"
    ):
        repair.repair04._verify_clean_source_provenance(fixture["source"])


def test_final_manifest_cas_never_overwrites_foreign_mutation(
    tmp_path, monkeypatch
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    manifest_path = fixture["run"] / "RUN_MANIFEST.json"
    foreign = b'{"foreign_manifest_owner":true}\n'

    def inject(label: str) -> None:
        if label == "write:RUN_MANIFEST.json:at_conditional_replace":
            manifest_path.write_bytes(foreign)

    monkeypatch.setattr(repair, "_transaction_fault_hook", inject)
    with pytest.raises(
        repair.Repair05RegistrationError, match="rollback was blocked"
    ):
        invoke(fixture)
    assert manifest_path.read_bytes() == foreign


def test_repair05_journal_and_progress_use_distinct_owner_schemas(
    tmp_path, monkeypatch
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    invoke(fixture)
    root = fixture["run"] / "DATA_INTEGRITY/repairs/repair-05"
    journal = json.loads((root / repair.TRANSACTION_JOURNAL).read_text())
    progress = json.loads((root / repair.REGISTRY_APPEND_PROGRESS).read_text())
    assert journal["schema_version"] == repair.JOURNAL_SCHEMA
    assert journal["repair_id"] == "repair-05"
    assert progress["schema_version"] == repair.REGISTRY_APPEND_PROGRESS_SCHEMA
    assert progress["repair_id"] == "repair-05"
    assert progress["state"] == "APPEND_COMPLETE"


def test_archive_copy_race_rejects_changed_resource_before_any_mutation(
    tmp_path, monkeypatch
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    manifest_before = (fixture["run"] / "RUN_MANIFEST.json").read_bytes()
    resource = fixture["run"] / repair.RESOURCE
    foreign = b'{"foreign_resource":true}\n'

    def inject(label: str) -> None:
        if label == "archive_copy:before":
            resource.write_bytes(foreign)

    monkeypatch.setattr(repair, "_transaction_fault_hook", inject)
    with pytest.raises(
        repair.Repair05RegistrationError,
        match="archive copy raced validation",
    ):
        invoke(fixture)
    assert resource.read_bytes() == foreign
    assert (fixture["run"] / "RUN_MANIFEST.json").read_bytes() == manifest_before
    assert not (fixture["run"] / "DATA_INTEGRITY/repairs/repair-05").exists()


@pytest.mark.parametrize(
    "target",
    ["cycle", "core", "w09", "parser", "resource", "preserved", "scratch"],
)
def test_idempotence_revalidates_every_immutable_boundary(
    tmp_path, monkeypatch, target
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    invoke(fixture)
    if target == "cycle":
        path = fixture["run"] / repair.CYCLE1_BINDING
    elif target == "core":
        path = fixture["run"] / "REPORT/CYCLE1_CORE_SUMMARY.json"
    elif target == "w09":
        path = fixture["run"] / repair.W09_ATTESTATION
    elif target == "parser":
        path = fixture["run"] / fixture["parent"]["parser_contract_path"]
    elif target == "resource":
        path = fixture["run"] / fixture["parent"]["resource_contract_path"]
    elif target == "preserved":
        path = fixture["attempt04_preserved"]
    else:
        path = fixture["run"] / repair.SCRATCH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"foreign-active-scratch")
    if target != "scratch":
        path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(repair.Repair05RegistrationError):
        invoke(fixture)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires a real child process")
@pytest.mark.parametrize(
    ("boundary", "missing_relative"),
    [
        (
            "write:SOURCE_MANIFEST.json:after_displace_before_publish",
            "SOURCE_MANIFEST.json",
        ),
        (
            "write:RUN_MANIFEST.json:after_displace_before_publish",
            "RUN_MANIFEST.json",
        ),
    ],
)
def test_sigkill_window_recovers_missing_active_and_retries_to_success(
    tmp_path, monkeypatch, boundary, missing_relative
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    pid = os.fork()
    if pid == 0:  # pragma: no cover - assertions happen in the parent
        def crash(label: str) -> None:
            if label == boundary:
                os._exit(77)

        repair._transaction_fault_hook = crash
        try:
            invoke(fixture)
        except BaseException:
            os._exit(78)
        os._exit(79)
    waited, status = os.waitpid(pid, 0)
    assert waited == pid
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 77
    assert not os.path.lexists(fixture["run"] / missing_relative)
    result = invoke(fixture)
    assert result["status"] == "REGISTRATION_REPAIR05_REFROZEN"
    manifest = json.loads((fixture["run"] / "RUN_MANIFEST.json").read_text())
    assert manifest["status"] == repair.POST_REPAIR_STATUS
    assert manifest["data_integrity_repairs"][-1]["repair_id"] == "repair-05"
    assert not list(
        (fixture["run"] / "DATA_INTEGRITY/repairs/repair-05").rglob(
            "*.original"
        )
    )


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires a real child process")
@pytest.mark.parametrize(
    "relative",
    ["SOURCE_MANIFEST.json", "RUN_MANIFEST.json"],
)
def test_recovery_of_recovery_keeps_active_atomic_then_retries(
    tmp_path, monkeypatch, relative
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    run = fixture["run"]

    first = os.fork()
    if first == 0:  # pragma: no cover - assertions happen in the parent
        repair._transaction_fault_hook = lambda label: (
            os._exit(81) if label == f"write:{relative}" else None
        )
        try:
            invoke(fixture)
        except BaseException:
            os._exit(82)
        os._exit(83)
    _, status = os.waitpid(first, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 81

    repair_dir = run / "DATA_INTEGRITY/repairs/repair-05"
    journal = json.loads((repair_dir / repair.TRANSACTION_JOURNAL).read_text())
    if relative == "RUN_MANIFEST.json":
        mutation = journal["manifest_mutation"]
    else:
        mutation = next(
            row for row in journal["active_mutations"]
            if row["path"] == relative
        )
    active = run / relative
    original = (run / mutation["original_archive_path"]).read_bytes()
    replacement = (run / mutation["replacement_staging_path"]).read_bytes()
    assert active.read_bytes() == replacement
    assert not os.path.lexists(run / mutation["displaced_path"])

    second = os.fork()
    if second == 0:  # pragma: no cover - assertions happen in the parent
        boundary = f"recovery:{relative}:after_restore_staging_before_replace"
        repair._transaction_fault_hook = lambda label: (
            os._exit(84) if label == boundary else None
        )
        try:
            repair.recover_incomplete_transaction(run, repair_dir)
        except BaseException:
            os._exit(85)
        os._exit(86)
    _, status = os.waitpid(second, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 84
    # Recovery itself crashed, but the live name never had an unlink gap.
    assert active.is_file()
    assert active.read_bytes() == replacement

    repair.recover_incomplete_transaction(run, repair_dir)
    assert active.read_bytes() == original
    assert not repair_dir.exists()
    assert not list(active.parent.glob(f".{active.name}.repair05-restore-*"))
    result = invoke(fixture)
    assert result["status"] == "REGISTRATION_REPAIR05_REFROZEN"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires a real child process")
@pytest.mark.parametrize("foreign_displaced", [False, True])
def test_committed_manifest_crash_window_is_cleaned_or_fails_foreign(
    tmp_path, monkeypatch, foreign_displaced
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    run = fixture["run"]
    boundary = "write:RUN_MANIFEST.json:after_publish_before_displaced_cleanup"
    pid = os.fork()
    if pid == 0:  # pragma: no cover - assertions happen in the parent
        repair._transaction_fault_hook = lambda label: (
            os._exit(87) if label == boundary else None
        )
        try:
            invoke(fixture)
        except BaseException:
            os._exit(88)
        os._exit(89)
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 87

    repair_dir = run / "DATA_INTEGRITY/repairs/repair-05"
    journal = json.loads((repair_dir / repair.TRANSACTION_JOURNAL).read_text())
    displaced = run / journal["manifest_mutation"]["displaced_path"]
    assert displaced.is_file()
    manifest = json.loads((run / "RUN_MANIFEST.json").read_text())
    assert len(manifest["data_integrity_repairs"]) == 5
    if foreign_displaced:
        displaced.write_bytes(b"foreign-displaced-manifest")
        with pytest.raises(
            repair.Repair05RegistrationError,
            match="committed manifest cleanup identity mismatch",
        ):
            invoke(fixture)
        assert displaced.read_bytes() == b"foreign-displaced-manifest"
    else:
        result = invoke(fixture)
        assert result["status"] == "REGISTRATION_REPAIR05_ALREADY_REFROZEN"
        assert not os.path.lexists(displaced)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires a real child process")
def test_registry_recovery_of_recovery_preserves_foreign_suffix(
    tmp_path, monkeypatch
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    run = fixture["run"]
    registry = run / "TRIAL_REGISTRY.jsonl"
    first = os.fork()
    if first == 0:  # pragma: no cover - assertions happen in the parent
        repair._transaction_fault_hook = lambda label: (
            os._exit(90)
            if label == "write:TRIAL_REGISTRY.jsonl"
            else None
        )
        try:
            invoke(fixture)
        except BaseException:
            os._exit(91)
        os._exit(92)
    _, status = os.waitpid(first, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 90

    repair_dir = run / "DATA_INTEGRITY/repairs/repair-05"
    journal = json.loads((repair_dir / repair.TRANSACTION_JOURNAL).read_text())
    registry_mutation = next(
        row for row in journal["active_mutations"]
        if row["append_only_registry"] is True
    )
    original = (run / registry_mutation["original_archive_path"]).read_bytes()
    foreign = repair.base.registry_line({
        "trial_registration_id": "FOREIGN_AFTER_REPAIR05_APPEND",
        "record_type": "FOREIGN",
    })
    with registry.open("ab") as handle:
        handle.write(foreign)
        handle.flush()
        os.fsync(handle.fileno())

    second = os.fork()
    if second == 0:  # pragma: no cover - assertions happen in the parent
        repair._transaction_fault_hook = lambda label: (
            os._exit(93)
            if label == "registry_recovery:after_replace_before_complete"
            else None
        )
        try:
            repair.recover_incomplete_transaction(run, repair_dir)
        except BaseException:
            os._exit(94)
        os._exit(95)
    _, status = os.waitpid(second, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 93
    assert registry.read_bytes() == original + foreign
    progress = json.loads(
        (repair_dir / repair.REGISTRY_APPEND_PROGRESS).read_text()
    )
    assert progress["rollback_state"] == "PREPARED"

    repair.recover_incomplete_transaction(run, repair_dir)
    assert registry.read_bytes() == original + foreign
    assert not repair_dir.exists()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires a real child process")
def test_registry_displaced_path_is_never_optional_recovery_inventory(
    tmp_path, monkeypatch
):
    fixture = make_transaction_fixture(tmp_path, monkeypatch)
    run = fixture["run"]
    pid = os.fork()
    if pid == 0:  # pragma: no cover - assertions happen in the parent
        repair._transaction_fault_hook = lambda label: (
            os._exit(96) if label == "publish_repair_dir" else None
        )
        try:
            invoke(fixture)
        except BaseException:
            os._exit(97)
        os._exit(98)
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 96

    repair_dir = run / "DATA_INTEGRITY/repairs/repair-05"
    journal = json.loads((repair_dir / repair.TRANSACTION_JOURNAL).read_text())
    registry_mutation = next(
        row for row in journal["active_mutations"]
        if row["append_only_registry"] is True
    )
    foreign = run / registry_mutation["displaced_path"]
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_bytes(b"foreign-registry-displaced")
    with pytest.raises(
        repair.Repair05RegistrationError,
        match="transaction file set mismatch",
    ):
        invoke(fixture)
    assert foreign.read_bytes() == b"foreign-registry-displaced"


def test_gitless_legacy_options_are_rejected(tmp_path):
    with pytest.raises(repair.Repair05RegistrationError, match="real-Git"):
        repair.repair05_registration(
            tmp_path / "run",
            tmp_path / "source",
            repo_root=tmp_path,
            execution_commit="a" * 40,
        )


def test_cli_source_loader_ignores_valid_sibling_pyc(tmp_path):
    isolated = tmp_path / "source"
    isolated.mkdir()
    for name in (
        "repair_registration.py",
        "repair02_registration.py",
        "repair03_registration.py",
        "repair04_registration.py",
        "repair05_registration.py",
    ):
        target = isolated / name
        target.write_bytes(Path(__file__).with_name(name).read_bytes())
    subprocess.run(
        [sys.executable, "-m", "py_compile", str(isolated / "repair04_registration.py")],
        check=True,
    )
    (isolated / "repair04_registration.py").write_text(
        "raise RuntimeError('source bytes were used')\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(isolated / "repair05_registration.py"), "--help"],
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "source bytes were used" in result.stderr
