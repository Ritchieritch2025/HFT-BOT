#!/usr/bin/env python3
"""Offline contracts for W09 MODE 1 degraded-evidence autoresearch."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
W09 = ROOT / "deploy" / "w09"
TOOLS = ROOT / "tools"
RESEARCH_TOOLS = TOOLS / "research"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(RESEARCH_TOOLS))
sys.path.insert(0, str(W09))

import research_data  # noqa: E402
import research_reference  # noqa: E402
from deep03_v3_common import MODE, validate_explicit_releases  # noqa: E402
from test_research_reference_consumer import (  # noqa: E402
    _store_for,
    build_release,
)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _degrade_manifest(manifest):
    manifest = copy.deepcopy(manifest)
    basis = copy.deepcopy(manifest["evidence"]["basis"])
    basis["downgrade_reasons"] = ["UNASSESSED_PENDING_PIPE_W03"]
    manifest["evidence"] = {
        "tier": "SEALED_DEGRADED_EVIDENCE",
        "basis": basis,
    }
    manifest["evidence_tier"] = "SEALED_DEGRADED_EVIDENCE"
    manifest["evidence_tier_basis"] = basis
    manifest["publication_state"]["evidence_tier"] = (
        "SEALED_DEGRADED_EVIDENCE"
    )
    manifest["publication_state"]["evidence_basis_sha256"] = (
        research_reference.canonical_sha256(basis)
    )
    state_sha = research_reference.canonical_sha256(
        manifest["publication_state"]
    )
    rid = "%s__v3ref__seal-%s__pub-%s" % (
        manifest["date"],
        manifest["source_seal"]["sha256"][:8],
        state_sha[:16],
    )
    manifest["release_id"] = rid
    manifest["publication_state_sha256"] = state_sha
    research_reference.validate_manifest(manifest, rid)
    return rid, manifest


def _degraded_release():
    _old_rid, manifest, source = build_release()
    rid, manifest = _degrade_manifest(manifest)
    return rid, manifest, source


def _materialize_degraded(tmp_path: Path) -> tuple[Path, str]:
    cache = tmp_path / "cache"
    old_rid, confirmation, source = build_release()
    store = _store_for((old_rid, confirmation, source))
    assert research_data.cmd_fetch(store, str(cache), old_rid, False) == 0
    rid, degraded = _degrade_manifest(confirmation)
    old_release = cache / "releases" / old_rid
    release = cache / "releases" / rid
    old_release.rename(release)
    manifest_raw = json.dumps(degraded, sort_keys=True, indent=2).encode() + b"\n"
    (release / "MANIFEST.json").write_bytes(manifest_raw)
    marker_path = release / ".VERIFIED.json"
    marker = json.loads(marker_path.read_text())
    marker.update({
        "release_id": rid,
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "publication_state_sha256": degraded["publication_state_sha256"],
        "evidence_tier": "SEALED_DEGRADED_EVIDENCE",
        "evidence_tier_basis": degraded["evidence_tier_basis"],
    })
    marker_path.write_text(json.dumps(marker, sort_keys=True, indent=2) + "\n")
    assert research_data.cmd_view(
        str(cache), include_non_confirmation=True
    ) == 0
    return cache, rid


def test_exploratory_canary_accepts_degraded_but_strict_stays_red(tmp_path):
    exploratory = _load(
        "w09_exploratory_canary_test",
        W09 / "exploratory_v3_query_canary.py",
    )
    strict = _load("w09_strict_canary_guard", W09 / "v3_query_canary.py")
    cache, rid = _materialize_degraded(tmp_path)
    exploratory_receipt = tmp_path / "exploratory.json"
    strict_receipt = tmp_path / "strict.json"

    assert exploratory.main([
        "--cache", str(cache),
        "--release", rid,
        "--receipt", str(exploratory_receipt),
    ]) == 0
    receipt = json.loads(exploratory_receipt.read_text())
    assert receipt["mode"] == MODE
    assert receipt["state"] == exploratory.PASS_STATE
    assert receipt["evidence_tier"] == "SEALED_DEGRADED_EVIDENCE"
    assert receipt["strict_acceptance_claimed"] is False
    assert receipt["result_class"] == "EXPLORATORY_ONLY_NOT_STRICT_ACCEPTANCE"
    assert receipt["rfq"] == "OFF"

    assert strict.main([
        "--cache", str(cache),
        "--release", rid,
        "--receipt", str(strict_receipt),
    ]) == 2
    assert json.loads(strict_receipt.read_text())["state"] == strict.REFUSED_STATE


def test_deep03_gate_accepts_degraded_only_as_explicit_mode1(tmp_path):
    cache, rid = _materialize_degraded(tmp_path)
    records = validate_explicit_releases(cache, [rid])
    assert records[0]["evidence_tier"] == "SEALED_DEGRADED_EVIDENCE"
    assert records[0]["rfq_included"] is False


def test_selector_is_contiguous_rfq_free_and_evidence_bounded(tmp_path):
    selector = _load(
        "w09_exploratory_selector_test",
        W09 / "exploratory_release_selector.py",
    )
    rid, manifest, _source = _degraded_release()
    location = tmp_path / "reference_manifests" / rid
    location.mkdir(parents=True)
    (location / "MANIFEST.json").write_text(json.dumps(manifest))

    selected = selector.select(
        tmp_path,
        start_date="2026-07-13",
        end_date="2026-07-13",
    )
    assert selected["mode"] == MODE
    assert selected["release_ids"] == [rid]
    assert selected["rfq"] == "OFF"
    assert len(selected["selection_sha256"]) == 64

    with pytest.raises(selector.SelectionError, match="missing dates"):
        selector.select(
            tmp_path,
            start_date="2026-07-12",
            end_date="2026-07-13",
        )


def test_autoresearch_cycle_runs_once_then_is_an_idempotent_noop(tmp_path, monkeypatch):
    automation = _load(
        "w09_exploratory_automation_test",
        W09 / "exploratory_autoresearch.py",
    )
    rid = "2026-07-13__v3ref__seal-aaaaaaaa__pub-bbbbbbbbbbbbbbbb"
    selection = {
        "schema_version": "w09-exploratory-v3-release-selection-v1",
        "mode": MODE,
        "strict_acceptance_claimed": False,
        "start_date": "2026-07-13",
        "end_date": "2026-07-13",
        "require_contiguous": True,
        "rfq": "OFF",
        "release_ids": [rid],
        "releases": [{
            "release_id": rid,
            "date": "2026-07-13",
            "object_bytes": 0,
        }],
    }
    selection["selection_sha256"] = hashlib.sha256(
        json.dumps(
            selection,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()
    commands: list[list[str]] = []
    invocation_id = "1" * 32
    monkeypatch.setenv("INVOCATION_ID", invocation_id)

    def fake_runner(command, **_kwargs):
        command = list(command)
        commands.append(command)
        script = Path(command[1]).name if len(command) > 1 else ""
        if script == "exploratory_release_selector.py":
            output = json.dumps(selection)
        elif script == "exploratory_v3_query_canary.py":
            receipt = Path(command[command.index("--receipt") + 1])
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(json.dumps({
                "state": "W09_V3_EXPLORATORY_QUERY_CANARY_PASS",
                "mode": MODE,
                "strict_acceptance_claimed": False,
                "release_id": rid,
                "rfq": "OFF",
            }))
            output = "canary pass"
        elif script == "deep03_v3_prepare.py":
            run_root = Path(command[command.index("--run-root") + 1])
            run_id = command[command.index("--run-id") + 1]
            (run_root / run_id).mkdir(parents=True)
            output = "prepare pass"
        elif script == "deep03_v3_runner.py":
            run_dir = Path(command[command.index("--run-dir") + 1])
            (run_dir / "RUN_COMPLETE.json").write_text(json.dumps({
                "state": "RUN_COMPLETE",
                "mode": MODE,
                "strict_acceptance_claimed": False,
                "rfq_reads": 0,
                "release_ids": [rid],
            }))
            output = "run pass"
        else:
            output = "ok"
        return subprocess.CompletedProcess(command, 0, stdout=output)

    kwargs = {
        "cache": tmp_path / "cache",
        "state_root": tmp_path / "state",
        "run_root": tmp_path / "runs",
        "start_date": "2026-07-13",
        "end_date": "2026-07-13",
        "python": sys.executable,
        "tools_root": TOOLS,
        "w09_tools_root": W09,
        "hook": tmp_path / "absent-hook",
        "authority_binding": {
            "state": "AUTHORIZED",
            "mode": MODE,
            "release_id": "D3-W2A-test-release",
            "authority_sha256": "a" * 64,
            "arm_sha256": "b" * 64,
            "arm_claim_state": "ACTIVE",
            "arm_claim_sha256": "d" * 64,
            "arm_claim_invocation_id": invocation_id,
            "arm_claim_service_unit": "w09-exploratory-autoresearch.service",
            "arm_claim_service_cgroup": (
                "/system.slice/w09-exploratory-autoresearch.service"
            ),
            "adopted_plan_sha256": "c" * 64,
            "max_runtime_seconds": 3600,
            "effective_runtime_seconds": 3600,
        },
        "required_release_ids": [rid],
        "authority_path": tmp_path / "authority.json",
        "arm_path": tmp_path / "arm.json",
        "arm_claim_root": tmp_path / "claims",
        "plan_path": tmp_path / "plan.md",
        "runtime_commit_path": tmp_path / "commit.txt",
        "audit_path": tmp_path / "audit.md",
        "w0_release_path": tmp_path / "w0.json",
        "w1_release_path": tmp_path / "w1.json",
        "w1_complete_path": tmp_path / "W1_COMPLETE.json",
        "runner": fake_runner,
    }
    first = automation.run_cycle(**kwargs)
    assert first["state"] == "RESEARCH_COMPLETE"
    assert first["idempotent_noop"] is False
    assert any(Path(row[1]).name == "deep03_v3_runner.py" for row in commands)
    runner_command = next(
        row for row in commands if Path(row[1]).name == "deep03_v3_runner.py"
    )
    assert runner_command[runner_command.index("--memory-limit") + 1] == "16GB"
    assert runner_command[runner_command.index("--threads") + 1] == "2"
    deep03_commands = [
        row
        for row in commands
        if len(row) > 1
        and Path(row[1]).name in {"deep03_v3_prepare.py", "deep03_v3_runner.py"}
    ]
    assert deep03_commands
    for command in deep03_commands:
        for flag in (
            "--authority",
            "--arm-file",
            "--arm-claim-root",
            "--plan",
            "--runtime-commit",
            "--audit",
            "--w0-release",
            "--w1-release",
            "--w1-complete",
        ):
            assert flag in command

    commands.clear()
    second = automation.run_cycle(**kwargs)
    assert second["state"] == "RESEARCH_COMPLETE"
    assert second["idempotent_noop"] is True
    assert not any(Path(row[1]).name == "deep03_v3_runner.py" for row in commands)

    commands.clear()
    unauthorized = dict(kwargs)
    unauthorized["state_root"] = tmp_path / "state-mismatch"
    unauthorized["required_release_ids"] = [
        "2026-07-14__v3ref__seal-cccccccc__pub-dddddddddddddddd"
    ]
    with pytest.raises(
        automation.AutoResearchError,
        match="differs from exact-release authority",
    ):
        automation.run_cycle(**unauthorized)
    assert not any(Path(row[1]).name == "deep03_v3_runner.py" for row in commands)


def test_deployment_payload_and_timer_are_pinned():
    manifest = W09 / "exploratory_autoresearch_payload.sha256"
    rows = []
    for line in manifest.read_text().splitlines():
        digest, relative = line.split("  ", 1)
        rows.append(relative)
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == digest
    assert set(rows) == {
        "deploy/w09/deep03_open_discovery_modules.sha256",
        "deploy/w09/deep03_authority_gate.py",
        "deploy/w09/deep03_one_shot_arm.py",
        "deploy/w09/exploratory_v3_query_canary.py",
        "deploy/w09/exploratory_release_selector.py",
        "deploy/w09/exploratory_autoresearch.py",
        "deploy/w09/w09-exploratory-autoresearch.service",
        "deploy/w09/w09-exploratory-autoresearch.timer",
        "deploy/w09/w09-inhibit-run",
        "deploy/w09/w09-run",
    }
    service = (W09 / "w09-exploratory-autoresearch.service").read_text()
    timer = (W09 / "w09-exploratory-autoresearch.timer").read_text()
    assert "deep03_authority_gate.py" in service
    assert "deep03_one_shot_arm.py claim" in service
    assert "deep03_one_shot_arm.py consume" in service
    assert "ExecStartPre=+" in service
    assert "ExecStopPost=+" in service
    assert "--arm-claim-root /var/lib/w09-deep03/one-shot" in service
    assert "--authority /etc/w09/deep03/AUTHORITY.json" in service
    assert "--runtime-commit /opt/w09/research/release-commit.txt" in service
    assert "--audit /etc/w09/deep03/audit.md" in service
    assert (
        "--w0-release /etc/w09/deep03/releases/D3-W0-20260718-06.json"
        in service
    )
    assert (
        "--w1-release /etc/w09/deep03/releases/D3-W1-20260718-06.json"
        in service
    )
    assert (
        "--w1-complete /etc/w09/deep03/prerequisites/W1_COMPLETE.json"
        in service
    )
    assert "exploratory_autoresearch.sha256" in service
    assert "OnUnitInactiveSec=30min" in timer
    assert "Persistent=true" in timer
    assert "--with-rfq" not in service
    installer = (W09 / "install_on_host.sh").read_text()
    assert "enable --now w09-exploratory-autoresearch.timer" not in installer
    assert "disable --now w09-exploratory-autoresearch.timer" in installer
    assert "AUTHORITY.json" not in installer
    assert "d3-w2a-execution-arm.json" not in installer
    assert "release-commit.txt" in installer
    wrapper = (W09 / "w09-run").read_text()
    inhibitor = (W09 / "w09-inhibit-run").read_text()
    assert '--systemd-invocation-id "$INVOCATION_ID"' in wrapper
    assert '/usr/bin/env INVOCATION_ID="$invocation_id"' in inhibitor
    assert "/usr/local/bin/w09-run" in installer
    assert "/usr/local/libexec/w09-inhibit-run" in installer
    autoresearch = (W09 / "exploratory_autoresearch.py").read_text()
    gate_source = (W09 / "deep03_authority_gate.py").read_text()
    assert 'default="/srv/w09-research/cache"' in autoresearch
    assert "cache-v3-exploratory" not in autoresearch
    assert "/srv/w09-research/cache" in gate_source


def test_static_credentials_are_refused_without_printing_values(tmp_path, monkeypatch):
    automation = _load(
        "w09_exploratory_credential_refusal_test",
        W09 / "exploratory_autoresearch.py",
    )
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "never-print-this-value")
    with pytest.raises(automation.AutoResearchError) as caught:
        automation.run_cycle(
            cache=tmp_path / "cache",
            state_root=tmp_path / "state",
            run_root=tmp_path / "runs",
            start_date="2026-07-13",
            end_date="2026-07-13",
            python=sys.executable,
            tools_root=TOOLS,
            w09_tools_root=W09,
            hook=tmp_path / "hook",
            authority_binding={},
            required_release_ids=["unused"],
            authority_path=tmp_path / "authority.json",
            arm_path=tmp_path / "arm.json",
            arm_claim_root=tmp_path / "claims",
            plan_path=tmp_path / "plan.md",
            runtime_commit_path=tmp_path / "commit.txt",
            audit_path=tmp_path / "audit.md",
            w0_release_path=tmp_path / "w0.json",
            w1_release_path=tmp_path / "w1.json",
            w1_complete_path=tmp_path / "W1_COMPLETE.json",
        )
    assert "never-print-this-value" not in str(caught.value)


def _authority_files(
    tmp_path: Path,
    release_ids: list[str] | None = None,
    *,
    expected_object_count: int | None = None,
    expected_object_bytes: int | None = None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    gate = _load("w09_deep03_authority_fixture", W09 / "deep03_authority_gate.py")
    if expected_object_count is not None:
        gate.EXPECTED_OBJECT_COUNT = expected_object_count
    if expected_object_bytes is not None:
        gate.EXPECTED_OBJECT_BYTES = expected_object_bytes
    plan = tmp_path / "adopted-plan.md"
    runtime = tmp_path / "release-commit.txt"
    authority_path = tmp_path / "AUTHORITY.json"
    arm_path = tmp_path / "arm.json"
    audit_path = tmp_path / "audit.md"
    w0_release_path = tmp_path / "D3-W0.json"
    w1_release_path = tmp_path / "D3-W1.json"
    w1_complete_path = tmp_path / "W1_COMPLETE.json"
    plan.write_text("# adopted audited deep03 plan\n")
    runtime.write_text("1" * 40 + "\n")
    operator_text = (
        "Authorize exact D3-W2A exploratory execution under "
        "D3-W2A-2026-07-18.06 for the named release set."
    )
    release_ids = release_ids or [
        (
            f"2026-07-{day:02d}__v3ref__seal-{day:08x}"
            f"__pub-{day:016x}"
        )
        for day in range(10, 18)
    ]
    release_dates = [release_id.split("__", 1)[0] for release_id in release_ids]
    w0_release_id = "D3-W0-2026-07-18.06"
    w1_release_id = "D3-W1-2026-07-18.06"
    audit_path.write_bytes(b"independent_plan_audit")
    plan_sha = hashlib.sha256(plan.read_bytes()).hexdigest()
    audit_sha = hashlib.sha256(audit_path.read_bytes()).hexdigest()
    w0_release_path.write_text(json.dumps({
        "schema_version": "deep03-w0-release-v1",
        "state": "RELEASE_CANDIDATE",
        "release_id": w0_release_id,
        "adopted_plan_sha256": plan_sha,
        "audit_sha256": audit_sha,
        "runtime_commit": "1" * 40,
        "expected_evidence_tier": gate.EXPECTED_EVIDENCE_TIER,
        "expected_object_count": gate.EXPECTED_OBJECT_COUNT,
        "expected_object_bytes": gate.EXPECTED_OBJECT_BYTES,
        "research_execution_authority": False,
    }, sort_keys=True) + "\n")
    w0_sha = hashlib.sha256(w0_release_path.read_bytes()).hexdigest()
    w1_release_path.write_text(json.dumps({
        "schema_version": "deep03-w1-release-v1",
        "state": "RELEASE_CANDIDATE",
        "release_id": w1_release_id,
        "w0_release_id": w0_release_id,
        "w0_release_sha256": w0_sha,
        "adopted_plan_sha256": plan_sha,
        "audit_sha256": audit_sha,
        "runtime_commit": "1" * 40,
        "mode": gate.MODE,
        "expected_evidence_tier": gate.EXPECTED_EVIDENCE_TIER,
        "expected_object_count": gate.EXPECTED_OBJECT_COUNT,
        "expected_object_bytes": gate.EXPECTED_OBJECT_BYTES,
        "authorized_input_release_ids": release_ids,
        "authorized_input_dates": release_dates,
        "rfq_included": False,
        "strict_acceptance_claimed": False,
        "holdout_opened": False,
        "research_execution_authority": False,
    }, sort_keys=True) + "\n")
    w1_sha = hashlib.sha256(w1_release_path.read_bytes()).hexdigest()
    dq = {
        "schema_version": gate.W1_DQ_SCHEMA,
        "state": "W1_DQ_PASS_FOR_EXPLORATORY_ONLY",
        "mode": gate.MODE,
        "evidence_tier": gate.EXPECTED_EVIDENCE_TIER,
        "strict_acceptance_claimed": False,
        "exact_version_local_verification": "PASS",
        "holdout_opened": False,
        "rfq": "OFF_AND_ABSENT",
        "release_ids": release_ids,
        "release_count": len(release_ids),
        "object_count": gate.EXPECTED_OBJECT_COUNT,
        "object_bytes": gate.EXPECTED_OBJECT_BYTES,
        "evidence_tier_counts": {
            gate.EXPECTED_EVIDENCE_TIER: len(release_ids)
        },
        "canaries": [
            {
                "release_id": release_id,
                "date": release_id.split("__", 1)[0],
                "state": gate.W1_CANARY_STATE,
                "evidence_tier": gate.EXPECTED_EVIDENCE_TIER,
                "sha256": hashlib.sha256(release_id.encode("ascii")).hexdigest(),
                "table_count": 3,
                "strict_acceptance_claimed": False,
                "rfq": "OFF",
            }
            for release_id in release_ids
        ],
    }
    dq_sha = hashlib.sha256(
        json.dumps(
            dq,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
    ).hexdigest()
    artifact_hashes = {
        "INPUT_MANIFEST.json": hashlib.sha256(b"input manifest").hexdigest(),
        "DATA_QUALITY_RECEIPT.json": dq_sha,
        "PRIOR_EXPOSURE_LEDGER.jsonl": hashlib.sha256(b"prior ledger").hexdigest(),
        "SPLIT_MANIFEST_OPEN_DISCOVERY.json": hashlib.sha256(b"split seal").hexdigest(),
    }
    w1_complete_path.write_text(json.dumps({
        "schema_version": gate.W1_COMPLETION_SCHEMA,
        "state": "W1_COMPLETE_EXPLORATORY_PRECHECK",
        "mode": gate.MODE,
        "w0_release_id": w0_release_id,
        "w0_release_sha256": w0_sha,
        "w1_release_id": w1_release_id,
        "w1_release_sha256": w1_sha,
        "adopted_plan_sha256": plan_sha,
        "audit_sha256": audit_sha,
        "authorized_input_release_ids": release_ids,
        "artifacts_sha256": artifact_hashes,
        "embedded_data_quality_receipt": dq,
        "release_count": len(release_ids),
        "evidence_tier": gate.EXPECTED_EVIDENCE_TIER,
        "object_count": gate.EXPECTED_OBJECT_COUNT,
        "object_bytes": gate.EXPECTED_OBJECT_BYTES,
        "all_inputs_prior_exposed": True,
        "holdout_opened": False,
        "strict_acceptance_claimed": False,
        "research_execution_started": False,
        "rfq": "OFF_AND_ABSENT",
        "network_reads_during_preflight": 0,
        "s3_writes": 0,
    }, sort_keys=True) + "\n")
    prerequisite_hashes = {
        "independent_plan_audit": audit_sha,
        "prior_exposure_ledger": artifact_hashes["PRIOR_EXPOSURE_LEDGER.jsonl"],
        "w09_exact_version_read": dq_sha,
        "w1_data_quality": dq_sha,
        "w1_completion": hashlib.sha256(w1_complete_path.read_bytes()).hexdigest(),
        "w1_input_manifest": artifact_hashes["INPUT_MANIFEST.json"],
        "w1_split_seal": artifact_hashes["SPLIT_MANIFEST_OPEN_DISCOVERY.json"],
    }
    authority = {
        "schema_version": gate.AUTHORITY_SCHEMA,
        "state": "ACTIVE",
        "release_id": "D3-W2A-2026-07-18.06",
        "issued_at_utc": "2026-07-18T12:00:00Z",
        "expires_at_utc": "2026-07-19T12:00:00Z",
        "operator_text_verbatim": operator_text,
        "operator_text_sha256": hashlib.sha256(
            operator_text.encode("utf-8")
        ).hexdigest(),
        "adopted_plan_path": gate.PLAN_INSTALL_PATH,
        "adopted_plan_sha256": plan_sha,
        "base_commit": "1" * 40,
        "authorized_phase_id": gate.PHASE,
        "authorized_work_package_id": gate.WORK_PACKAGE,
        "authorized_instance_id": gate.INSTANCE_ID,
        "authorized_role": gate.ROLE,
        "mode": gate.MODE,
        "execution_class": "EXPLORATORY_ONLY",
        "expected_evidence_tier": gate.EXPECTED_EVIDENCE_TIER,
        "expected_object_count": gate.EXPECTED_OBJECT_COUNT,
        "expected_object_bytes": gate.EXPECTED_OBJECT_BYTES,
        "authorized_write_roots": sorted(gate.WRITE_ROOTS),
        "allowed_network_operations": gate.NETWORK_OPERATIONS,
        "active_prompt_state": "EXPLICIT_NONE",
        "active_prompt_path": None,
        "active_prompt_sha256": None,
        "named_supersessions": [gate.REQUIRED_NAMED_SUPERSESSION],
        "authorized_source_state": "DETACHED_EXACT_COMMIT",
        "authorized_branch": None,
        "authorized_worktree": "/Users/ritcardo/HFT-BOT-deep03-runtime-release",
        "authorized_tool_classes": gate.AUTHORIZED_TOOL_CLASSES,
        "authorized_api_classes": gate.AUTHORIZED_API_CLASSES,
        "authorized_credential_classes": gate.AUTHORIZED_CREDENTIAL_CLASSES,
        "prerequisite_receipt_sha256s": prerequisite_hashes,
        "audit_sha256": audit_sha,
        "independent_audit_verdict": "PASS_WITH_EXPLICIT_BLOCKERS",
        "w0_release_id": w0_release_id,
        "w0_release_sha256": w0_sha,
        "w1_release_id": w1_release_id,
        "w1_release_sha256": w1_sha,
        "session_count": 1,
        "authorized_method_scope": gate.AUTHORIZED_METHOD_SCOPE,
        "authorized_input_release_ids": release_ids,
        "input_start_date": release_dates[0],
        "input_end_date": release_dates[-1],
        "spending_cap_usd": 15.0,
        "max_runtime_seconds": 3600,
        **{field: False for field in gate.FALSE_AUTHORITY_FIELDS},
    }
    authority_path.write_text(json.dumps(authority, sort_keys=True) + "\n")
    authority_sha = hashlib.sha256(authority_path.read_bytes()).hexdigest()
    arm = {
        "schema_version": gate.ARM_SCHEMA,
        "state": "ARMED",
        "release_id": authority["release_id"],
        "authority_sha256": authority_sha,
        "base_commit": authority["base_commit"],
        "authorized_work_package_id": gate.WORK_PACKAGE,
        "mode": gate.MODE,
        "armed_at_utc": "2026-07-18T12:01:00Z",
        "expires_at_utc": "2026-07-19T11:00:00Z",
    }
    arm_path.write_text(json.dumps(arm, sort_keys=True) + "\n")
    for path in (
        plan,
        runtime,
        authority_path,
        arm_path,
        audit_path,
        w0_release_path,
        w1_release_path,
        w1_complete_path,
    ):
        path.chmod(0o444)
    return (
        gate,
        authority_path,
        arm_path,
        plan,
        runtime,
        audit_path,
        w0_release_path,
        w1_release_path,
        w1_complete_path,
    )


def _claimed_authority_files(
    tmp_path: Path,
    release_ids: list[str] | None = None,
    *,
    expected_object_count: int | None = None,
    expected_object_bytes: int | None = None,
):
    files = _authority_files(
        tmp_path,
        release_ids,
        expected_object_count=expected_object_count,
        expected_object_bytes=expected_object_bytes,
    )
    gate, authority, arm_path, *_rest = files
    one_shot = _load(
        "w09_deep03_one_shot_fixture", W09 / "deep03_one_shot_arm.py"
    )
    claim_root = tmp_path / "one-shot"
    claim_root.mkdir(mode=0o750)
    invocation_id = "1" * 32
    proc_cgroup = tmp_path / "proc-self-cgroup"
    proc_cgroup.write_text(
        "0::/system.slice/w09-exploratory-autoresearch.service\n"
    )
    identity = one_shot.load_arm_identity(
        authority_path=authority,
        arm_path=arm_path,
        expected_owner_uid=os.getuid(),
    )
    one_shot.claim_one_shot(
        claim_root=claim_root,
        identity=identity,
        invocation_id=invocation_id,
        expected_owner_uid=os.getuid(),
        proc_cgroup_path=proc_cgroup,
    )
    return (*files, claim_root, invocation_id, proc_cgroup)


def _rewrite_authority_and_arm(authority_path: Path, arm_path: Path, mutate) -> None:
    authority = json.loads(authority_path.read_text())
    mutate(authority)
    authority_path.chmod(0o644)
    authority_path.write_text(json.dumps(authority, sort_keys=True) + "\n")
    authority_path.chmod(0o444)
    arm = json.loads(arm_path.read_text())
    arm["authority_sha256"] = hashlib.sha256(authority_path.read_bytes()).hexdigest()
    arm_path.chmod(0o644)
    arm_path.write_text(json.dumps(arm, sort_keys=True) + "\n")
    arm_path.chmod(0o444)


def _validate_authority_fixture(files):
    gate, authority, arm, plan, runtime, audit, w0, w1, w1_complete = files
    return gate.validate_authority(
        authority_path=authority,
        arm_path=arm,
        plan_path=plan,
        runtime_commit_path=runtime,
        audit_path=audit,
        w0_release_path=w0,
        w1_release_path=w1,
        w1_complete_path=w1_complete,
        expected_owner_uid=os.getuid(),
        now=dt.datetime(2026, 7, 18, 13, tzinfo=dt.timezone.utc),
    )


def test_exact_release_authority_and_arm_bind_plan_runtime_and_input(tmp_path):
    gate, authority, arm, plan, runtime, audit, w0, w1, w1_complete = _authority_files(tmp_path)
    result, artifacts = gate.validate_authority_bundle(
        authority_path=authority,
        arm_path=arm,
        plan_path=plan,
        runtime_commit_path=runtime,
        audit_path=audit,
        w0_release_path=w0,
        w1_release_path=w1,
        w1_complete_path=w1_complete,
        expected_owner_uid=os.getuid(),
        now=dt.datetime(2026, 7, 18, 13, tzinfo=dt.timezone.utc),
    )
    assert result["state"] == "AUTHORIZED"
    assert result["release_id"] == "D3-W2A-2026-07-18.06"
    assert len(result["authorized_input_release_ids"]) == 8
    assert result["authorized_input_release_ids"][0].startswith("2026-07-10__")
    assert result["base_commit"] == "1" * 40
    assert result["authorized_source_state"] == "DETACHED_EXACT_COMMIT"
    assert result["authorized_branch"] is None
    assert result["expected_evidence_tier"] == "SEALED_DEGRADED_EVIDENCE"
    assert result["expected_object_count"] == 2657
    assert result["expected_object_bytes"] == 29473216651
    assert result["w09_exact_version_read_evidence"] == {
        "binding_kind": "COMPOSITE_W1_DATA_QUALITY_RECEIPT_SHA256",
        "sha256": result["prerequisite_receipt_sha256s"][
            "w09_exact_version_read"
        ],
        "exact_version_local_verification": "PASS",
        "canary_state": gate.W1_CANARY_STATE,
        "canary_count": 8,
        "evidence_tier": gate.EXPECTED_EVIDENCE_TIER,
        "object_count": gate.EXPECTED_OBJECT_COUNT,
        "object_bytes": gate.EXPECTED_OBJECT_BYTES,
    }
    assert result["spending_cap_usd"] == 15.0
    assert result["required_max_runtime_cost_usd"] == 0.50918
    assert set(artifacts) == {
        "ADOPTED_PLAN.md",
        "AUDIT.md",
        "AUTHORITY.json",
        "BASE_COMMIT.txt",
        "D3_W0_RELEASE.json",
        "D3_W1_RELEASE.json",
        "EXECUTION_ARM.json",
        "W1_COMPLETE.json",
    }
    assert hashlib.sha256(artifacts["AUDIT.md"]).hexdigest() == result[
        "audit_sha256"
    ]
    for name in ("D3_W0_RELEASE.json", "D3_W1_RELEASE.json"):
        upstream = json.loads(artifacts[name])
        assert upstream["expected_evidence_tier"] == result[
            "expected_evidence_tier"
        ]
        assert upstream["expected_object_count"] == result[
            "expected_object_count"
        ]
        assert upstream["expected_object_bytes"] == result[
            "expected_object_bytes"
        ]


def test_claimed_authority_requires_current_systemd_one_shot(tmp_path):
    (
        gate,
        authority,
        arm,
        plan,
        runtime,
        audit,
        w0,
        w1,
        w1_complete,
        claim_root,
        invocation_id,
        proc_cgroup,
    ) = _claimed_authority_files(tmp_path)
    result, artifacts = gate.validate_claimed_authority_bundle(
        authority_path=authority,
        arm_path=arm,
        arm_claim_root=claim_root,
        plan_path=plan,
        runtime_commit_path=runtime,
        audit_path=audit,
        w0_release_path=w0,
        w1_release_path=w1,
        w1_complete_path=w1_complete,
        expected_owner_uid=os.getuid(),
        invocation_id=invocation_id,
        proc_cgroup_path=proc_cgroup,
        now=dt.datetime(2026, 7, 18, 13, tzinfo=dt.timezone.utc),
    )
    assert result["arm_claim_state"] == "ACTIVE"
    assert result["arm_claim_invocation_id"] == invocation_id
    assert result["arm_claim_service_cgroup"] == (
        "/system.slice/w09-exploratory-autoresearch.service"
    )
    assert hashlib.sha256(artifacts["ARM_CLAIM.json"]).hexdigest() == result[
        "arm_claim_sha256"
    ]
    with pytest.raises(gate.AuthorityError, match="invocation_id"):
        gate.validate_claimed_authority(
            authority_path=authority,
            arm_path=arm,
            arm_claim_root=claim_root,
            plan_path=plan,
            runtime_commit_path=runtime,
            audit_path=audit,
            w0_release_path=w0,
            w1_release_path=w1,
            w1_complete_path=w1_complete,
            expected_owner_uid=os.getuid(),
            invocation_id="2" * 32,
            proc_cgroup_path=proc_cgroup,
            now=dt.datetime(2026, 7, 18, 13, tzinfo=dt.timezone.utc),
        )
    proc_cgroup.write_text(
        "0::/user.slice/user-1000.slice/user@1000.service/app.slice/ssh-session.scope\n"
    )
    with pytest.raises(gate.AuthorityError, match="outside the fixed W09 service cgroup"):
        gate.validate_claimed_authority(
            authority_path=authority,
            arm_path=arm,
            arm_claim_root=claim_root,
            plan_path=plan,
            runtime_commit_path=runtime,
            audit_path=audit,
            w0_release_path=w0,
            w1_release_path=w1,
            w1_complete_path=w1_complete,
            expected_owner_uid=os.getuid(),
            invocation_id=invocation_id,
            proc_cgroup_path=proc_cgroup,
            now=dt.datetime(2026, 7, 18, 13, tzinfo=dt.timezone.utc),
        )


def test_authority_gate_refuses_missing_mutable_or_unbound_arm(tmp_path):
    gate, authority, arm, plan, runtime, audit, w0, w1, w1_complete = _authority_files(tmp_path)
    now = dt.datetime(2026, 7, 18, 13, tzinfo=dt.timezone.utc)
    arm.chmod(0o644)
    with pytest.raises(gate.AuthorityError, match="writable"):
        gate.validate_authority(
            authority_path=authority,
            arm_path=arm,
            plan_path=plan,
            runtime_commit_path=runtime,
            audit_path=audit,
            w0_release_path=w0,
            w1_release_path=w1,
            w1_complete_path=w1_complete,
            expected_owner_uid=os.getuid(),
            now=now,
        )
    arm.chmod(0o444)
    arm.unlink()
    with pytest.raises(gate.AuthorityError, match="missing or unsafe"):
        gate.validate_authority(
            authority_path=authority,
            arm_path=arm,
            plan_path=plan,
            runtime_commit_path=runtime,
            audit_path=audit,
            w0_release_path=w0,
            w1_release_path=w1,
            w1_complete_path=w1_complete,
            expected_owner_uid=os.getuid(),
            now=now,
        )


@pytest.mark.parametrize(
    ("artifact_name", "message"),
    [
        ("audit", "independent audit bytes differ"),
        ("w0", "D3-W0 release bytes differ"),
        ("w1", "D3-W1 release bytes differ"),
        ("w1_complete", "W1_COMPLETE bytes differ"),
    ],
)
def test_authority_gate_refuses_prerequisite_byte_drift(
    tmp_path, artifact_name, message
):
    files = _authority_files(tmp_path)
    gate, authority, arm, plan, runtime, audit, w0, w1, w1_complete = files
    targets = {
        "audit": audit,
        "w0": w0,
        "w1": w1,
        "w1_complete": w1_complete,
    }
    target = targets[artifact_name]
    target.chmod(0o644)
    target.write_bytes(target.read_bytes() + b" ")
    target.chmod(0o444)
    with pytest.raises(gate.AuthorityError, match=message):
        gate.validate_authority(
            authority_path=authority,
            arm_path=arm,
            plan_path=plan,
            runtime_commit_path=runtime,
            audit_path=audit,
            w0_release_path=w0,
            w1_release_path=w1,
            w1_complete_path=w1_complete,
            expected_owner_uid=os.getuid(),
            now=dt.datetime(2026, 7, 18, 13, tzinfo=dt.timezone.utc),
        )


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("active_prompt_state", None, "active_prompt_state"),
        ("named_supersessions", [], "section 4.15.1"),
        ("authorized_source_state", "UNKNOWN", "authorized_source_state"),
        ("authorized_branch", "", "authorized_branch"),
        ("authorized_tool_classes", [], "authorized_tool_classes"),
        ("authorized_api_classes", [], "authorized_api_classes"),
        ("authorized_credential_classes", [], "authorized_credential_classes"),
        ("prerequisite_receipt_sha256s", {}, "incomplete keys"),
        ("w0_release_id", "D3-W2A-wrong", "W0 release ID"),
        ("w1_release_sha256", "bad", "W1 release SHA-256"),
        ("session_count", 2, "session_count"),
        ("authorized_method_scope", {}, "partial W2A scope"),
        ("expected_evidence_tier", "SEALED_CONFIRMATION", "field mismatch"),
        ("expected_object_count", 2656, "field mismatch"),
        ("expected_object_bytes", 29473216650, "field mismatch"),
        ("s3_write_permission", True, "explicit false"),
    ],
)
def test_authority_gate_refuses_missing_or_broadened_narrow_scope(
    tmp_path, field, replacement, message
):
    gate, authority_path, arm, plan, runtime, audit, w0, w1, w1_complete = _authority_files(tmp_path)
    authority = json.loads(authority_path.read_text())
    authority[field] = replacement
    authority_path.chmod(0o644)
    authority_path.write_text(json.dumps(authority, sort_keys=True) + "\n")
    authority_path.chmod(0o444)
    arm_value = json.loads(arm.read_text())
    arm_value["authority_sha256"] = hashlib.sha256(authority_path.read_bytes()).hexdigest()
    arm.chmod(0o644)
    arm.write_text(json.dumps(arm_value, sort_keys=True) + "\n")
    arm.chmod(0o444)
    with pytest.raises(gate.AuthorityError, match=message):
        gate.validate_authority(
            authority_path=authority_path,
            arm_path=arm,
            plan_path=plan,
            runtime_commit_path=runtime,
            audit_path=audit,
            w0_release_path=w0,
            w1_release_path=w1,
            w1_complete_path=w1_complete,
            expected_owner_uid=os.getuid(),
            now=dt.datetime(2026, 7, 18, 13, tzinfo=dt.timezone.utc),
        )


@pytest.mark.parametrize(
    ("cap", "runtime", "message"),
    [
        (15.01, 3600, "must be in \\(0,15]"),
        (0.50, 3600, "does not cover max_runtime_seconds"),
        (12.22031, 86400, "does not cover max_runtime_seconds"),
    ],
)
def test_authority_gate_enforces_hard_cap_and_runtime_cost(
    tmp_path, cap, runtime, message
):
    files = _authority_files(tmp_path)
    _gate, authority, arm, *_rest = files
    _rewrite_authority_and_arm(
        authority,
        arm,
        lambda value: value.update(
            {"spending_cap_usd": cap, "max_runtime_seconds": runtime}
        ),
    )
    with pytest.raises(files[0].AuthorityError, match=message):
        _validate_authority_fixture(files)


def test_authority_gate_accepts_full_day_when_cap_covers_fixed_rate(tmp_path):
    files = _authority_files(tmp_path)
    _gate, authority, arm, *_rest = files
    _rewrite_authority_and_arm(
        authority,
        arm,
        lambda value: value.update(
            {"spending_cap_usd": 15.0, "max_runtime_seconds": 86400}
        ),
    )
    result = _validate_authority_fixture(files)
    assert result["required_max_runtime_cost_usd"] == 12.22032


def test_authority_gate_accepts_named_or_detached_exact_source_identity(tmp_path):
    files = _authority_files(tmp_path)
    result = _validate_authority_fixture(files)
    assert result["authorized_source_state"] == "DETACHED_EXACT_COMMIT"
    assert result["authorized_branch"] is None

    _gate, authority, arm, *_rest = files
    _rewrite_authority_and_arm(
        authority,
        arm,
        lambda value: value.update({
            "authorized_source_state": "NAMED_BRANCH",
            "authorized_branch": "w-deep03-release-01",
        }),
    )
    result = _validate_authority_fixture(files)
    assert result["authorized_source_state"] == "NAMED_BRANCH"
    assert result["authorized_branch"] == "w-deep03-release-01"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("adopted_plan_sha256", "0" * 64),
        ("audit_sha256", "0" * 64),
        ("w0_release_id", "D3-W0-wrong"),
        ("w0_release_sha256", "1" * 64),
        ("w1_release_id", "D3-W1-wrong"),
        ("w1_release_sha256", "2" * 64),
        ("authorized_input_release_ids", []),
    ],
)
def test_authority_gate_cross_binds_w1_completion_identity_fields(
    tmp_path, field, replacement
):
    files = _authority_files(tmp_path)
    gate, authority, arm, _plan, _runtime, _audit, _w0, _w1, complete = files
    value = json.loads(complete.read_text())
    value[field] = replacement
    complete.chmod(0o644)
    complete.write_text(json.dumps(value, sort_keys=True) + "\n")
    complete.chmod(0o444)
    _rewrite_authority_and_arm(
        authority,
        arm,
        lambda document: document["prerequisite_receipt_sha256s"].update({
            "w1_completion": hashlib.sha256(complete.read_bytes()).hexdigest()
        }),
    )
    with pytest.raises(gate.AuthorityError, match="W1_COMPLETE field mismatch"):
        _validate_authority_fixture(files)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("evidence_tier", "SEALED_CONFIRMATION"),
        ("object_count", 2656),
        ("object_bytes", 29473216650),
    ],
)
def test_authority_gate_cross_binds_w1_completion_semantics(
    tmp_path, field, replacement
):
    files = _authority_files(tmp_path)
    gate, authority, arm, _plan, _runtime, _audit, _w0, _w1, complete = files
    value = json.loads(complete.read_text())
    value[field] = replacement
    complete.chmod(0o644)
    complete.write_text(json.dumps(value, sort_keys=True) + "\n")
    complete.chmod(0o444)
    _rewrite_authority_and_arm(
        authority,
        arm,
        lambda document: document["prerequisite_receipt_sha256s"].update({
            "w1_completion": hashlib.sha256(complete.read_bytes()).hexdigest()
        }),
    )
    with pytest.raises(gate.AuthorityError, match="W1_COMPLETE field mismatch"):
        _validate_authority_fixture(files)


def test_authority_gate_cross_binds_w1_completion_and_prerequisite_map(tmp_path):
    files = _authority_files(tmp_path)
    gate, authority, arm, _plan, _runtime, _audit, _w0, _w1, complete = files
    value = json.loads(complete.read_text())
    value["artifacts_sha256"]["INPUT_MANIFEST.json"] = "f" * 64
    complete.chmod(0o644)
    complete.write_text(json.dumps(value, sort_keys=True) + "\n")
    complete.chmod(0o444)

    def update_authority(document):
        document["prerequisite_receipt_sha256s"]["w1_completion"] = (
            hashlib.sha256(complete.read_bytes()).hexdigest()
        )

    _rewrite_authority_and_arm(authority, arm, update_authority)
    with pytest.raises(gate.AuthorityError, match="INPUT_MANIFEST.json"):
        _validate_authority_fixture(files)


def test_authority_gate_parses_composite_dq_exact_pass_and_eight_canaries(tmp_path):
    files = _authority_files(tmp_path)
    gate, authority, arm, _plan, _runtime, _audit, _w0, _w1, complete = files
    value = json.loads(complete.read_text())
    dq = value["embedded_data_quality_receipt"]
    dq["exact_version_local_verification"] = "UNKNOWN"
    dq_sha = hashlib.sha256(
        json.dumps(
            dq,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
    ).hexdigest()
    value["artifacts_sha256"]["DATA_QUALITY_RECEIPT.json"] = dq_sha
    complete.chmod(0o644)
    complete.write_text(json.dumps(value, sort_keys=True) + "\n")
    complete.chmod(0o444)

    def update_authority(document):
        hashes = document["prerequisite_receipt_sha256s"]
        hashes["w1_completion"] = hashlib.sha256(complete.read_bytes()).hexdigest()
        hashes["w1_data_quality"] = dq_sha
        hashes["w09_exact_version_read"] = dq_sha

    _rewrite_authority_and_arm(authority, arm, update_authority)
    with pytest.raises(gate.AuthorityError, match="exact_version_local_verification"):
        _validate_authority_fixture(files)


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("evidence_tier", "SEALED_CONFIRMATION", "DQ field mismatch"),
        ("object_count", 2656, "DQ field mismatch"),
        ("object_bytes", 29473216650, "DQ field mismatch"),
        (
            "evidence_tier_counts",
            {"SEALED_CONFIRMATION": 8},
            "evidence tier counts differ",
        ),
    ],
)
def test_authority_gate_refuses_dq_semantic_drift(
    tmp_path, field, replacement, message
):
    files = _authority_files(tmp_path)
    gate, authority, arm, _plan, _runtime, _audit, _w0, _w1, complete = files
    value = json.loads(complete.read_text())
    dq = value["embedded_data_quality_receipt"]
    dq[field] = replacement
    dq_sha = hashlib.sha256(
        json.dumps(
            dq,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
    ).hexdigest()
    value["artifacts_sha256"]["DATA_QUALITY_RECEIPT.json"] = dq_sha
    complete.chmod(0o644)
    complete.write_text(json.dumps(value, sort_keys=True) + "\n")
    complete.chmod(0o444)

    def update_authority(document):
        hashes = document["prerequisite_receipt_sha256s"]
        hashes["w1_completion"] = hashlib.sha256(complete.read_bytes()).hexdigest()
        hashes["w1_data_quality"] = dq_sha
        hashes["w09_exact_version_read"] = dq_sha

    _rewrite_authority_and_arm(authority, arm, update_authority)
    with pytest.raises(gate.AuthorityError, match=message):
        _validate_authority_fixture(files)


def test_authority_gate_refuses_canary_evidence_tier_drift(tmp_path):
    files = _authority_files(tmp_path)
    gate, authority, arm, _plan, _runtime, _audit, _w0, _w1, complete = files
    value = json.loads(complete.read_text())
    dq = value["embedded_data_quality_receipt"]
    dq["canaries"][0]["evidence_tier"] = "SEALED_CONFIRMATION"
    dq_sha = hashlib.sha256(
        json.dumps(
            dq,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
    ).hexdigest()
    value["artifacts_sha256"]["DATA_QUALITY_RECEIPT.json"] = dq_sha
    complete.chmod(0o644)
    complete.write_text(json.dumps(value, sort_keys=True) + "\n")
    complete.chmod(0o444)

    def update_authority(document):
        hashes = document["prerequisite_receipt_sha256s"]
        hashes["w1_completion"] = hashlib.sha256(complete.read_bytes()).hexdigest()
        hashes["w1_data_quality"] = dq_sha
        hashes["w09_exact_version_read"] = dq_sha

    _rewrite_authority_and_arm(authority, arm, update_authority)
    with pytest.raises(gate.AuthorityError, match="canary contract mismatch"):
        _validate_authority_fixture(files)


def test_authority_gate_refuses_composite_dq_without_all_eight_canaries(tmp_path):
    files = _authority_files(tmp_path)
    gate, authority, arm, _plan, _runtime, _audit, _w0, _w1, complete = files
    value = json.loads(complete.read_text())
    dq = value["embedded_data_quality_receipt"]
    dq["canaries"].pop()
    dq_sha = hashlib.sha256(
        json.dumps(
            dq,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
    ).hexdigest()
    value["artifacts_sha256"]["DATA_QUALITY_RECEIPT.json"] = dq_sha
    complete.chmod(0o644)
    complete.write_text(json.dumps(value, sort_keys=True) + "\n")
    complete.chmod(0o444)

    def update_authority(document):
        hashes = document["prerequisite_receipt_sha256s"]
        hashes["w1_completion"] = hashlib.sha256(complete.read_bytes()).hexdigest()
        hashes["w1_data_quality"] = dq_sha
        hashes["w09_exact_version_read"] = dq_sha

    _rewrite_authority_and_arm(authority, arm, update_authority)
    with pytest.raises(gate.AuthorityError, match="exactly eight canaries"):
        _validate_authority_fixture(files)


def test_authority_gate_requires_exact_read_prerequisite_to_equal_composite_dq(
    tmp_path,
):
    files = _authority_files(tmp_path)
    gate, authority, arm, *_rest = files
    _rewrite_authority_and_arm(
        authority,
        arm,
        lambda value: value["prerequisite_receipt_sha256s"].update(
            {"w09_exact_version_read": "e" * 64}
        ),
    )
    with pytest.raises(gate.AuthorityError, match="composite W1 DQ"):
        _validate_authority_fixture(files)
