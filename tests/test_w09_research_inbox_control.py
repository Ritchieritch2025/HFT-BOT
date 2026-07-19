#!/usr/bin/env python3
"""Offline contracts for W09's fixed Research Inbox control helper."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from research.inbox import create_job  # noqa: E402
from research.plan_contract import compile_plan  # noqa: E402


def _control_module():
    source = ROOT / "deploy" / "w09" / "research_inbox_control.py"
    spec = importlib.util.spec_from_file_location("research_inbox_control_test", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plan() -> str:
    return "\n".join(
        [
            "---",
            "plugin: deep03",
            "data:",
            "  required: [L1, L2, TRADES, MARKET_GRAPH]",
            "  forbidden: [RFQ]",
            "---",
            "# W09 control fixture",
            "",
        ]
    )


def _local_job(root: Path):
    text = _plan()
    return create_job(
        root,
        filename="PLAN.md",
        plan_text=text,
        job_spec=compile_plan(text, "PLAN.md"),
    )


def _bundle(control, source: Path, job_id: str) -> str:
    files = []
    for name in control.CONTROL_FILES:
        raw = (source / name).read_bytes()
        files.append({"name": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    manifest = {
        "schema_version": control.SCHEMA_BUNDLE,
        "job_id": job_id,
        "files": files,
        "data_files_transferred": 0,
        "data_access": control.DATA_PLANE,
    }
    return hashlib.sha256(control._canonical(manifest)).hexdigest()


def _prepare_upload_commit(control, tmp_path):
    local = tmp_path / "local"
    job = _local_job(local)
    job_id = job["job_id"]
    source = local / "jobs" / job_id
    remote = tmp_path / "remote"
    remote.mkdir()
    bundle = _bundle(control, source, job_id)
    control.prepare_receive(remote, job_id, bundle)
    incoming = remote / ".incoming" / job_id
    for name in control.CONTROL_FILES:
        shutil.copyfile(source / name, incoming / name)
    receipt = control.commit_receive(remote, job_id, bundle)
    return local, remote, job_id, bundle, receipt


def test_receive_is_four_file_sha_bound_atomic_commit(tmp_path):
    control = _control_module()
    _local, remote, job_id, bundle, receipt = _prepare_upload_commit(control, tmp_path)

    final = remote / "jobs" / job_id
    assert receipt["state"] == "COMMITTED_NOT_STARTED"
    assert receipt["bundle_sha256"] == bundle
    assert receipt["data_files_received"] == 0
    assert sorted(path.name for path in final.iterdir()) == sorted(control.CONTROL_FILES)
    assert not (remote / ".incoming" / job_id).exists()
    state = json.loads((remote / ".control" / (job_id + ".json")).read_text())
    assert state["state"] == "COMMITTED"
    assert set(state["immutable_sha256s"]) == {"PLAN.md", "JOB_SPEC.json", "REQUEST.json"}


def test_commit_refuses_symlink_drift_credentials_and_wrong_bundle(tmp_path):
    control = _control_module()
    local = tmp_path / "local"
    job = _local_job(local)
    job_id = job["job_id"]
    source = local / "jobs" / job_id
    remote = tmp_path / "remote"
    remote.mkdir()
    bundle = _bundle(control, source, job_id)
    control.prepare_receive(remote, job_id, bundle)
    incoming = remote / ".incoming" / job_id
    for name in control.CONTROL_FILES:
        shutil.copyfile(source / name, incoming / name)
    (incoming / "PLAN.md").unlink()
    (incoming / "PLAN.md").symlink_to(source / "PLAN.md")
    with pytest.raises(control.ResearchInboxControlError, match="symlink|regular"):
        control.commit_receive(remote, job_id, bundle)
    assert not (remote / "jobs" / job_id).exists()

    (incoming / "PLAN.md").unlink()
    shutil.copyfile(source / "PLAN.md", incoming / "PLAN.md")
    spec_path = incoming / "JOB_SPEC.json"
    spec_path.write_bytes(spec_path.read_bytes() + b" ")
    with pytest.raises(control.ResearchInboxControlError, match="canonical|bundle"):
        control.commit_receive(remote, job_id, bundle)

    spec_path.write_bytes((source / "JOB_SPEC.json").read_bytes())
    request_path = incoming / "REQUEST.json"
    request = json.loads(request_path.read_text())
    request["note"] = "AKIA" + "A" * 16
    request_path.write_text(
        json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    )
    with pytest.raises(control.ResearchInboxControlError, match="credential-like"):
        control.commit_receive(remote, job_id, bundle)
    with pytest.raises(control.ResearchInboxControlError, match="SHA-256"):
        control.commit_receive(remote, job_id, "not-a-sha")


def test_explicit_start_arm_guards_one_fixed_systemd_instance(tmp_path):
    control = _control_module()
    _local, remote, job_id, bundle, _receipt = _prepare_upload_commit(control, tmp_path)
    run_root = tmp_path / "run"
    calls = []

    def runner(command, **kwargs):
        assert kwargs["shell"] is False
        calls.append(command)
        verified = control.verify_start_arm(remote, run_root, job_id)
        assert verified["bundle_sha256"] == bundle
        return subprocess.CompletedProcess(command, 0, b"", b"")

    receipt = control.start_committed_job(
        remote,
        run_root,
        job_id,
        bundle,
        runner=runner,
        require_root=False,
    )

    assert receipt["state"] == "GENERIC_PLANNING_WORKER_COMPLETED"
    assert receipt["research_execution_started"] is False
    assert calls == [
        [
            "/usr/bin/systemctl",
            "start",
            "w09-research-inbox-worker@%s.service" % job_id,
        ]
    ]
    assert not (run_root / (job_id + ".json")).exists()
    with pytest.raises(control.ResearchInboxControlError, match="missing|unsafe"):
        control.verify_start_arm(remote, run_root, job_id)


def test_export_is_read_only_bounded_status_results_report_tar(tmp_path):
    control = _control_module()
    _local, remote, job_id, _bundle_sha, _receipt = _prepare_upload_commit(control, tmp_path)
    final = remote / "jobs" / job_id
    results = {
        "schema_version": "research-results-v1",
        "job_id": job_id,
        "title": "Result",
    }
    results_raw = json.dumps(results, sort_keys=True, separators=(",", ":")).encode()
    report_raw = b"<!doctype html><title>Result</title>"
    receipt = {
        "schema_version": "research-report-receipt-v1",
        "job_id": job_id,
        "results_sha256": hashlib.sha256(results_raw).hexdigest(),
        "report_sha256": hashlib.sha256(report_raw).hexdigest(),
    }
    status = {
        "schema_version": "research-job-status-v1",
        "job_id": job_id,
        "state": "COMPLETE",
        "research_execution_started": True,
        "report_ready": True,
    }
    (final / "RESULTS.json").write_bytes(results_raw)
    (final / "REPORT").mkdir()
    (final / "REPORT" / "index.html").write_bytes(report_raw)
    (final / "REPORT" / "REPORT_RECEIPT.json").write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    )
    status_stage = final / ".STATUS.worker-stage"
    status_stage.write_text(
        json.dumps(status, sort_keys=True, separators=(",", ":")) + "\n"
    )
    status_stage.replace(final / "STATUS.json")

    raw = control.export_outputs(remote, job_id)

    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        names = sorted(member.name for member in archive.getmembers())
        assert names == [
            "REPORT/REPORT_RECEIPT.json",
            "REPORT/index.html",
            "RESULTS.json",
            "STATUS.json",
        ]
        assert all(member.isfile() and not member.issym() for member in archive.getmembers())
    assert (final / "PLAN.md").is_file()
    assert (final / "RESULTS.json").read_bytes() == results_raw


def test_template_is_planning_only_arm_gated_and_not_timer_enabled():
    service = (ROOT / "deploy" / "w09" / "w09-research-inbox-worker@.service").read_text()
    wrapper = (ROOT / "deploy" / "w09" / "w09-research-inbox-control").read_text()
    sudoers = (ROOT / "deploy" / "w09" / "w09-research-inbox-control.sudoers").read_text()
    assert "research_inbox.sha256" in service
    assert "verify-start" in service
    assert "research_job_worker.py" in service
    assert "/opt/w09/research/deploy/w09/research_job_worker.py" in service
    assert "--job-id %i" in service
    assert "--inbox-root /srv/w09-research/inbox" in service
    assert "--cache-root /srv/w09-research/cache" in service
    assert "IPAddressDeny=any" in service
    assert "ExecStart" in service and "deep03-v3-run" not in service
    assert "/opt/w09/research/tools/research_inbox_control.py" in wrapper
    assert sudoers.strip() == (
        "ubuntu ALL=(root) NOPASSWD: "
        "/usr/local/libexec/w09-research-inbox-control start *"
    )


def test_research_inbox_payload_and_installer_are_complete_and_hash_pinned():
    w09 = ROOT / "deploy" / "w09"
    manifest = w09 / "research_inbox_payload.sha256"
    rows = {}
    for line in manifest.read_text().splitlines():
        digest, relative = line.split("  ", 1)
        rows[relative] = digest
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == digest
    assert set(rows) == {
        "deploy/w09/research_inbox_control.py",
        "deploy/w09/research_job_worker.py",
        "deploy/w09/w09-research-inbox-control",
        "deploy/w09/w09-research-inbox-control.sudoers",
        "deploy/w09/w09-research-inbox-worker@.service",
        "tools/research/inbox.py",
        "tools/research/plan_contract.py",
        "tools/research/data_catalog.py",
        "tools/research/data_resolver.py",
        "tools/research/plugin_api.py",
        "tools/research/plugins/__init__.py",
        "tools/research/plugins/deep03.py",
    }

    installer = (w09 / "install_on_host.sh").read_text()
    push = (w09 / "push_and_install.sh").read_text()
    assert "sha256sum -c" in installer and "research_inbox_payload.sha256" in installer
    assert "shasum -a 256 -c" in push and "research_inbox_payload.sha256" in push
    assert "/etc/w09/research_inbox.sha256" in installer
    assert '"$INSTALL_ROOT/deploy/w09/research_job_worker.py"' in installer
    assert '"$INSTALL_ROOT/tools/research_reference.py"' in installer
    for name in (
        "research_job_worker.py",
        "research_inbox_control.py",
        "w09-research-inbox-control",
        "w09-research-inbox-control.sudoers",
        "w09-research-inbox-worker@.service",
        "plugin_api.py",
        "plugins/deep03.py",
    ):
        assert name in installer
        assert name in push
    assert "enable --now w09-research-inbox" not in installer
    assert "start w09-research-inbox" not in installer
    assert not list(w09.glob("*research-inbox*.timer"))
