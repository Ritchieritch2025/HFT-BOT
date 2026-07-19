#!/usr/bin/env python3
"""Offline contracts for the Mac Research Inbox -> W09 controller."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from research.inbox import create_job  # noqa: E402
from research.plan_contract import compile_plan  # noqa: E402
from research.w09_dispatch import (  # noqa: E402
    CONTROL_FILES,
    W09DispatchError,
    W09_INTERFACE,
    dispatch_job,
    pull_job_outputs,
    start_job,
)


def _plan() -> str:
    return "\n".join(
        [
            "---",
            "plugin: deep03",
            "data:",
            "  required: [L1, L2, TRADES, MARKET_GRAPH]",
            "  forbidden: [RFQ]",
            "budget:",
            "  max_runtime_seconds: 3600",
            "  max_spend_usd: 2",
            "---",
            "# Generic spread experiment",
            "",
        ]
    )


def _job(root: Path, text: str | None = None):
    text = text or _plan()
    spec = compile_plan(text, "PLAN.md")
    return create_job(
        root,
        filename="PLAN.md",
        plan_text=text,
        job_spec=spec,
        automatic_execution_requested=True,
    )


def _key(root: Path) -> Path:
    key = root / "w09-test-key.pem"
    key.write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "offline-fixture-not-a-real-key\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
    )
    key.chmod(0o600)
    return key


class Recorder:
    def __init__(self, export: bytes = b""):
        self.commands: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.export = export

    def __call__(self, command, **kwargs):
        assert isinstance(command, list)
        assert kwargs["shell"] is False
        self.commands.append(command)
        self.kwargs.append(kwargs)
        stdout = self.export if "export" in command else b""
        return subprocess.CompletedProcess(command, 0, stdout, b"")


def _tar(files: dict[str, bytes], *, link: tuple[str, str] | None = None) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        if link is not None:
            info = tarfile.TarInfo(link[0])
            info.type = tarfile.SYMTYPE
            info.linkname = link[1]
            archive.addfile(info)
    return output.getvalue()


def _complete_outputs(job_id: str) -> tuple[bytes, dict[str, bytes]]:
    results = {
        "schema_version": "research-results-v1",
        "job_id": job_id,
        "title": "Generic spread experiment",
        "state": "COMPLETE",
        "execution_class": "READONLY_EXPLORATORY",
        "summary": ["Descriptive result."],
        "metrics": [],
        "tables": [],
        "limitations": ["Open discovery."],
        "receipts": {},
    }
    results_raw = json.dumps(results, sort_keys=True, separators=(",", ":")).encode()
    report_raw = b"<!doctype html><title>Research result</title>"
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
        "updated_at_utc": "2026-07-19T00:00:00Z",
        "research_execution_started": True,
        "report_ready": True,
        "next_action": None,
        "message": "Complete.",
    }
    files = {
        "STATUS.json": (
            json.dumps(status, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode(),
        "RESULTS.json": results_raw,
        "REPORT/index.html": report_raw,
        "REPORT/REPORT_RECEIPT.json": json.dumps(
            receipt, sort_keys=True, separators=(",", ":")
        ).encode(),
    }
    artifacts = [
        {
            "path": name,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for name, payload in sorted(files.items())
        if name != "STATUS.json"
    ]
    output_receipt = {
        "schema_version": "research-job-output-receipt-v1",
        "job_id": job_id,
        "output_directory": "OUTPUT",
        "artifact_count": len(artifacts),
        "artifact_bytes": sum(row["bytes"] for row in artifacts),
        "artifacts": artifacts,
        "data_files_exported": 0,
        "source_contract": "DEDICATED_JOB_OUTPUT_ONLY",
    }
    unsigned = (
        json.dumps(output_receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode()
    output_receipt["output_sha256"] = hashlib.sha256(unsigned).hexdigest()
    files["OUTPUT_RECEIPT.json"] = (
        json.dumps(output_receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode()
    return _tar(files), files


def test_dispatch_commits_only_four_controls_and_never_starts(tmp_path):
    inbox = tmp_path / "inbox"
    job = _job(inbox)
    key = _key(tmp_path)
    recorder = Recorder()

    receipt = dispatch_job(inbox, job["job_id"], ssh_key=key, runner=recorder)

    assert receipt["state"] == "COMMITTED_NOT_STARTED"
    assert receipt["research_started"] is False
    assert receipt["data_files_transferred"] == 0
    assert receipt["data_access"] == "W09_INSTANCE_PROFILE_EXACT_VERSION_READONLY"
    assert [command[0] for command in recorder.commands] == ["ssh", "scp", "ssh"]
    assert "receive-prepare" in recorder.commands[0]
    assert "receive-commit" in recorder.commands[2]
    assert not any("start" in command for command in recorder.commands)
    scp = recorder.commands[1]
    separator = scp.index("--")
    sources = scp[separator + 1 : -1]
    assert tuple(Path(path).name for path in sources) == CONTROL_FILES
    assert len(sources) == 4
    assert scp[-1] == (
        "ubuntu@18.226.151.192:/srv/w09-research/inbox/.incoming/%s/"
        % job["job_id"]
    )
    for command in recorder.commands:
        assert "BatchMode=yes" in command
        assert "StrictHostKeyChecking=yes" in command
        assert str(key) in command
    assert not list((inbox / "jobs").glob(".*.dispatch.*"))


def test_start_is_a_separate_fixed_helper_action_bound_to_bundle(tmp_path):
    inbox = tmp_path / "inbox"
    job = _job(inbox)
    key = _key(tmp_path)
    dispatch_recorder = Recorder()
    dispatch = dispatch_job(inbox, job["job_id"], ssh_key=key, runner=dispatch_recorder)
    start_recorder = Recorder()

    receipt = start_job(
        inbox,
        job["job_id"],
        bundle_sha256=dispatch["bundle_sha256"],
        ssh_key=key,
        runner=start_recorder,
    )

    assert receipt["state"] == "START_REQUEST_ACCEPTED"
    assert len(start_recorder.commands) == 1
    command = start_recorder.commands[0]
    assert command[0] == "ssh"
    assert str(W09_INTERFACE.control_helper) in command
    assert "start" in command
    assert W09_INTERFACE.worker_unit_template not in command
    assert dispatch["bundle_sha256"] in command
    with pytest.raises(W09DispatchError, match="bundle SHA-256 differs"):
        start_job(
            inbox,
            job["job_id"],
            bundle_sha256="0" * 64,
            ssh_key=key,
            runner=Recorder(),
        )


def test_dispatch_rejects_links_credentials_and_source_drift(tmp_path):
    key = _key(tmp_path)
    inbox = tmp_path / "linked-inbox"
    job = _job(inbox)
    spec = inbox / "jobs" / job["job_id"] / "JOB_SPEC.json"
    outside = tmp_path / "outside.json"
    outside.write_bytes(spec.read_bytes())
    spec.unlink()
    spec.symlink_to(outside)
    with pytest.raises(W09DispatchError, match="symlink"):
        dispatch_job(inbox, job["job_id"], ssh_key=key, runner=Recorder())

    credential_inbox = tmp_path / "credential-inbox"
    text = "# Credential accident\nAKIA" + "A" * 16 + "\n"
    safe_spec = compile_plan("# Safe\n", "PLAN.md")
    safe_spec.update(
        {
            "plan_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "plan_bytes": len(text.encode()),
        }
    )
    credential_job = create_job(
        credential_inbox,
        filename="PLAN.md",
        plan_text=text,
        job_spec=safe_spec,
    )
    with pytest.raises(W09DispatchError, match="credential-like"):
        dispatch_job(
            credential_inbox,
            credential_job["job_id"],
            ssh_key=key,
            runner=Recorder(),
        )

    drift_inbox = tmp_path / "drift-inbox"
    drift_job = _job(drift_inbox)
    status_path = drift_inbox / "jobs" / drift_job["job_id"] / "STATUS.json"

    class DriftAfterPrepare(Recorder):
        def __call__(self, command, **kwargs):
            result = super().__call__(command, **kwargs)
            if "receive-prepare" in command:
                status_path.write_bytes(status_path.read_bytes() + b" ")
            return result

    drift = DriftAfterPrepare()
    with pytest.raises(W09DispatchError, match="drifted"):
        dispatch_job(drift_inbox, drift_job["job_id"], ssh_key=key, runner=drift)
    assert not any(command[0] == "scp" for command in drift.commands)
    assert any("receive-abort" in command for command in drift.commands)


def test_pull_installs_one_validated_snapshot_with_status_last(tmp_path):
    inbox = tmp_path / "inbox"
    job = _job(inbox)
    key = _key(tmp_path)
    archive, files = _complete_outputs(job["job_id"])
    recorder = Recorder(archive)

    receipt = pull_job_outputs(inbox, job["job_id"], ssh_key=key, runner=recorder)

    job_dir = inbox / "jobs" / job["job_id"]
    assert receipt["remote_state"] == "COMPLETE"
    assert receipt["installed"] == [
        "RESULTS.json",
        "REPORT",
        "W09_OUTPUT_RECEIPT.json",
        "STATUS.json",
    ]
    assert (job_dir / "STATUS.json").read_bytes() == files["STATUS.json"]
    assert (job_dir / "RESULTS.json").read_bytes() == files["RESULTS.json"]
    assert (job_dir / "REPORT" / "index.html").read_bytes() == files[
        "REPORT/index.html"
    ]
    assert (job_dir / "W09_OUTPUT_RECEIPT.json").read_bytes() == files[
        "OUTPUT_RECEIPT.json"
    ]
    assert len(recorder.commands) == 1
    command = recorder.commands[0]
    assert command[0] == "ssh" and "export" in command
    assert "STATUS_RESULTS_REPORT_RECEIPT" in command
    assert not list((inbox / "jobs").glob(".*.pull.*"))


@pytest.mark.parametrize(
    "archive,error",
    [
        (_tar({"../STATUS.json": b"{}"}), "path traversal"),
        (
            _tar({"STATUS.json": b"{}"}, link=("REPORT/index.html", "/etc/passwd")),
            "link or special",
        ),
        (
            _tar(
                {
                    "STATUS.json": (
                        b'{"schema_version":"research-job-status-v1",'
                        b'"job_id":"RJOB-20260718T235901123456Z-aaaaaaaaaaaa",'
                        b'"state":"RUNNING","note":"AKIAAAAAAAAAAAAAAAAA"}'
                    )
                }
            ),
            "credential-like",
        ),
    ],
)
def test_pull_rejects_unsafe_remote_archives_without_local_mutation(
    tmp_path, archive, error
):
    inbox = tmp_path / "inbox"
    job = _job(inbox)
    key = _key(tmp_path)
    status = inbox / "jobs" / job["job_id"] / "STATUS.json"
    before = status.read_bytes()
    with pytest.raises(W09DispatchError, match=error):
        pull_job_outputs(inbox, job["job_id"], ssh_key=key, runner=Recorder(archive))
    assert status.read_bytes() == before
    assert not (status.parent / "RESULTS.json").exists()
    assert not (status.parent / "REPORT").exists()


def test_pull_refuses_local_status_drift_and_weak_key_permissions(tmp_path):
    inbox = tmp_path / "inbox"
    job = _job(inbox)
    key = _key(tmp_path)
    status = inbox / "jobs" / job["job_id"] / "STATUS.json"
    archive, _ = _complete_outputs(job["job_id"])

    class DriftDuringExport(Recorder):
        def __call__(self, command, **kwargs):
            result = super().__call__(command, **kwargs)
            status.write_bytes(status.read_bytes() + b" ")
            return result

    with pytest.raises(W09DispatchError, match="drifted during remote export"):
        pull_job_outputs(
            inbox, job["job_id"], ssh_key=key, runner=DriftDuringExport(archive)
        )
    assert not (status.parent / "RESULTS.json").exists()

    key.chmod(0o644)
    assert stat.S_IMODE(key.stat().st_mode) == 0o644
    with pytest.raises(W09DispatchError, match="permissions"):
        dispatch_job(inbox, job["job_id"], ssh_key=key, runner=Recorder())


def test_invalid_job_id_never_reaches_subprocess(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    recorder = Recorder()
    with pytest.raises(W09DispatchError, match="invalid research job id"):
        dispatch_job(inbox, "../../etc/passwd", ssh_key=_key(tmp_path), runner=recorder)
    assert recorder.commands == []
