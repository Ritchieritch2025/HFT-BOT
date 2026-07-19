#!/usr/bin/env python3
"""Offline contracts for W09's fixed Research Inbox control helper."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import threading
import urllib.request

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from research.data_catalog import canonical_sha256  # noqa: E402
import research.inbox as inbox_module  # noqa: E402
from research.inbox import create_job, get_job, read_report, update_status  # noqa: E402
from research.plan_contract import compile_plan  # noqa: E402
from research.w09_dispatch import (  # noqa: E402
    dispatch_job,
    pull_job_outputs,
    start_job,
)
import dashboard_server  # noqa: E402


def _control_module():
    source = ROOT / "deploy" / "w09" / "research_inbox_control.py"
    spec = importlib.util.spec_from_file_location("research_inbox_control_test", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _worker_module():
    source = ROOT / "deploy" / "w09" / "research_job_worker.py"
    spec = importlib.util.spec_from_file_location("research_job_worker_flow_test", source)
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


def _key(root: Path) -> Path:
    path = root / "w09-test-key.pem"
    path.write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "offline-fixture-not-a-real-key\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
    )
    path.chmod(0o600)
    return path


def _catalog() -> dict:
    release_id = "2026-07-13__v3ref__seal-1234abcd__pub-1234567890abcdef"
    value = {
        "schema_version": "research-data-catalog-v1",
        "state": "READY",
        "release_count": 1,
        "dates": ["2026-07-13"],
        "releases": [
            {
                "release_id": release_id,
                "date": "2026-07-13",
                "published_at_utc": "2026-07-14T00:10:00Z",
                "storage_mode": "REFERENCE_V3",
                "version_binding_mode": "CANONICAL_REFERENCE",
                "manifest_sha256": "1" * 64,
                "manifest_version_id": "exact-manifest-version",
                "reference_set_sha256": "2" * 64,
                "object_semantics_sha256": "3" * 64,
                "publication_state_sha256": "4" * 64,
                "evidence_tier": "SEALED_DEGRADED_EVIDENCE",
                "tl1_status": "UNASSESSED_PENDING_PIPE_W03",
                "data_families": ["L1", "L2", "TRADES", "MARKET_GRAPH"],
                "channels": ["orderbooks_full", "orderbooks_l1", "trades"],
                "manifest_data_families": ["L1", "L2", "TRADES", "MARKET_GRAPH"],
                "manifest_channels": ["orderbooks_full", "orderbooks_l1", "trades"],
                "object_count": 17,
                "object_bytes": 123456,
                "manifest_object_count": 17,
                "manifest_object_bytes": 123456,
                "rfq": {
                    "manifest_included": False,
                    "materialized": False,
                    "status": "ABSENT_FROM_RELEASE",
                },
            }
        ],
        "rejected_releases": [],
        "network_reads": 0,
        "copied_bytes": 0,
        "zero_copy": True,
    }
    value["catalog_sha256"] = canonical_sha256(value)
    return value


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
    assert state["initial_status_sha256"] == hashlib.sha256(
        (final / "STATUS.json").read_bytes()
    ).hexdigest()
    assert state["last_status_state"] == "QUEUED"
    assert state["incoming_prepared"] is False


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
        update_status(remote, job_id, state="PREFLIGHT", message="planning")
        update_status(remote, job_id, state="READY", message="planned")
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
    assert receipt["planning_status"] == "READY"
    assert calls == [
        [
            "/usr/bin/systemctl",
            "start",
            "w09-research-inbox-worker@%s.service" % job_id,
        ]
    ]
    assert not (run_root / (job_id + ".json")).exists()
    state = json.loads((remote / ".control" / (job_id + ".json")).read_text())
    assert state["state"] == "WORKER_INVOKED"
    assert state["last_status_state"] == "READY"
    assert (remote / ".control" / (job_id + ".json")).stat().st_mode & 0o777 == 0o644
    with pytest.raises(control.ResearchInboxControlError, match="missing|unsafe"):
        control.verify_start_arm(remote, run_root, job_id)


def test_export_is_read_only_bounded_status_results_report_tar(tmp_path):
    control = _control_module()
    worker = _worker_module()
    _local, remote, job_id, bundle_sha, _receipt = _prepare_upload_commit(control, tmp_path)
    final = remote / "jobs" / job_id

    def planning_runner(command, **kwargs):
        control.verify_start_arm(remote, tmp_path / "run", job_id)
        worker.build_catalog = lambda _cache: _catalog()
        planned = worker.plan_job(
            inbox_root=remote, cache_root=tmp_path / "cache", job_id=job_id
        )
        assert planned["state"] == "READY"
        return subprocess.CompletedProcess(command, 0, b"", b"")

    control.start_committed_job(
        remote,
        tmp_path / "run",
        job_id,
        bundle_sha,
        runner=planning_runner,
        require_root=False,
    )
    report_raw = b"<!doctype html><title>Result</title>"
    update_status(remote, job_id, state="RUNNING", message="running")
    inputs = worker._job_inputs(remote, job_id)
    bindings = worker._planning_provenance_bindings(inputs)
    provenance = {
        "schema_version": worker.PROVENANCE_SCHEMA,
        **{
            key: bindings[key]
            for key in (
                "job_id",
                "plugin_id",
                "plan_sha256",
                "job_spec_sha256",
                "catalog_sha256",
                "selection_sha256",
                "preflight_sha256",
                "plugin_source_sha256",
                "planning_receipt_sha256",
                "execution_request_artifact_sha256",
                "execution_request_digest",
                "authority_schema",
                "arm_schema",
            )
        },
        "authority_sha256": "a" * 64,
        "arm_sha256": "b" * 64,
        "adopted_plan_sha256": inputs["plan_sha256"],
        "source_run_id": "offline-fixture-run",
        "source_run_complete_sha256": "c" * 64,
        "source_results_sha256": "d" * 64,
        "source_report_sha256": hashlib.sha256(report_raw).hexdigest(),
    }
    provenance["provenance_sha256"] = hashlib.sha256(
        worker._artifact_bytes(provenance)
    ).hexdigest()
    results = {
        "schema_version": "research-results-v1",
        "job_id": job_id,
        "title": "Result",
        "execution_provenance": provenance,
    }
    results_raw = json.dumps(results, sort_keys=True, separators=(",", ":")).encode()
    receipt = {
        "schema_version": "research-report-receipt-v1",
        "job_id": job_id,
        "results_sha256": hashlib.sha256(results_raw).hexdigest(),
        "report_sha256": hashlib.sha256(report_raw).hexdigest(),
    }
    source = tmp_path / "completed-output"
    source.mkdir()
    (source / "RESULTS.json").write_bytes(results_raw)
    (source / "REPORT").mkdir()
    (source / "REPORT" / "index.html").write_bytes(report_raw)
    (source / "REPORT" / "REPORT_RECEIPT.json").write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    )
    published = worker._publish_validated_job_output(
        inbox_root=remote,
        job_id=job_id,
        source_root=source,
        execution_provenance=provenance,
    )
    assert published["state"] == "OUTPUT_PUBLISHED"
    assert published["idempotent_replay"] is False
    update_status(remote, job_id, state="COMPLETE", message="complete")

    raw = control.export_outputs(remote, job_id)

    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        names = sorted(member.name for member in archive.getmembers())
        assert names == [
            "OUTPUT_RECEIPT.json",
            "REPORT/REPORT_RECEIPT.json",
            "REPORT/index.html",
            "RESULTS.json",
            "STATUS.json",
        ]
        assert all(member.isfile() and not member.issym() for member in archive.getmembers())
    assert (final / "PLAN.md").is_file()
    assert not (final / "RESULTS.json").exists()
    assert (final / "OUTPUT" / "RESULTS.json").read_bytes() == results_raw


def test_deep03_adapter_finalize_export_pull_report_e2e(tmp_path):
    """A completed authority-bound run becomes one generic dashboard report."""
    control = _control_module()
    worker = _worker_module()
    local, remote, job_id, bundle_sha, _receipt = _prepare_upload_commit(
        control, tmp_path
    )

    def planning_runner(command, **kwargs):
        control.verify_start_arm(remote, tmp_path / "run-arm", job_id)
        worker.build_catalog = lambda _cache: _catalog()
        assert worker.plan_job(
            inbox_root=remote, cache_root=tmp_path / "cache", job_id=job_id
        )["state"] == "READY"
        return subprocess.CompletedProcess(command, 0, b"", b"")

    control.start_committed_job(
        remote,
        tmp_path / "run-arm",
        job_id,
        bundle_sha,
        runner=planning_runner,
        require_root=False,
    )
    update_status(remote, job_id, state="RUNNING", message="authority gate started")
    inputs = worker._job_inputs(remote, job_id)
    bindings = worker._planning_provenance_bindings(inputs)
    releases = bindings["release_ids"]
    run_id = "deep03-offline-e2e"
    run_dir = tmp_path / run_id
    run_dir.mkdir()

    authority = {
        "schema_version": bindings["authority_schema"],
        "release_id": "D3-W2A-offline-e2e",
        "base_commit": "1" * 40,
        "adopted_plan_sha256": inputs["plan_sha256"],
        "authorized_input_release_ids": releases,
        "authorized_phase_id": "OPEN_DISCOVERY",
        "authorized_work_package_id": "D3-W2A",
    }
    authority_raw = worker._artifact_bytes(authority)
    arm = {
        "schema_version": bindings["arm_schema"],
        "release_id": authority["release_id"],
        "base_commit": authority["base_commit"],
        "authority_sha256": hashlib.sha256(authority_raw).hexdigest(),
    }
    authority_files = {
        "ADOPTED_PLAN.md": (remote / "jobs" / job_id / "PLAN.md").read_bytes(),
        "AUDIT.md": b"offline independent audit fixture\n",
        "AUTHORITY.json": authority_raw,
        "BASE_COMMIT.txt": (authority["base_commit"] + "\n").encode(),
        "D3_W0_RELEASE.json": b'{"release_id":"D3-W0-offline"}\n',
        "D3_W1_RELEASE.json": b'{"release_id":"D3-W1-offline"}\n',
        "EXECUTION_ARM.json": worker._artifact_bytes(arm),
        "W1_COMPLETE.json": b'{"state":"COMPLETE"}\n',
    }
    for name, raw in authority_files.items():
        (run_dir / name).write_bytes(raw)
    authority_hashes = {
        name: hashlib.sha256(raw).hexdigest()
        for name, raw in authority_files.items()
    }
    authority_binding = {
        "authority_sha256": authority_hashes["AUTHORITY.json"],
        "arm_sha256": authority_hashes["EXECUTION_ARM.json"],
        "adopted_plan_sha256": inputs["plan_sha256"],
        "authorized_input_release_ids": releases,
        "authorized_phase_id": "OPEN_DISCOVERY",
        "authorized_work_package_id": "D3-W2A",
    }
    (run_dir / "AUTHORITY_BINDING.json").write_bytes(
        worker._artifact_bytes(
            {
                "schema_version": "deep03-d3-w2a-authority-binding-v1",
                "binding": authority_binding,
                "artifact_sha256s": authority_hashes,
            }
        )
    )
    input_manifest = {
        "schema_version": "deep03-d3-w2a-v3-input-manifest-v1",
        "run_id": run_id,
        "release_ids": releases,
        "authority_binding": authority_binding,
        "authority_artifact_sha256s": authority_hashes,
    }
    (run_dir / "INPUT_MANIFEST.json").write_bytes(worker._artifact_bytes(input_manifest))
    deep_results = {
        "schema_version": worker.DEEP03_RESULTS_SCHEMA,
        "run_id": run_id,
        "mode": "MODE 1 / EXPLORATORY_AUTORESEARCH",
        "research_stage": "OPEN_DISCOVERY",
        "work_package": "D3-W2A",
        "release_ids": releases,
        "strict_acceptance_claimed": False,
        "candidate_or_profit_claim": False,
        "methods": [],
    }
    (run_dir / "RESULTS.json").write_bytes(worker._artifact_bytes(deep_results))
    (run_dir / "REPORT").mkdir()
    original_report = b"<!doctype html><title>Deep03 offline E2E</title>"
    (run_dir / "REPORT" / "index.html").write_bytes(original_report)
    for name in worker.DEEP03_RECEIPTS:
        (run_dir / name).write_bytes(
            worker._artifact_bytes({"schema_version": name.lower(), "run_id": run_id})
        )
    artifact_rows = []
    for path in sorted(run_dir.rglob("*")):
        if path.is_file():
            raw = path.read_bytes()
            artifact_rows.append(
                {
                    "path": path.relative_to(run_dir).as_posix(),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "size": len(raw),
                }
            )
    sums_raw = "".join(
        "%s  %s\n" % (row["sha256"], row["path"]) for row in artifact_rows
    ).encode()
    (run_dir / "ARTIFACT_SHA256SUMS").write_bytes(sums_raw)
    complete = {
        "schema_version": worker.DEEP03_COMPLETE_SCHEMA,
        "state": "RUN_COMPLETE",
        "exit_status": 0,
        "run_id": run_id,
        "mode": "MODE 1 / EXPLORATORY_AUTORESEARCH",
        "research_stage": "OPEN_DISCOVERY",
        "work_package": "D3-W2A",
        "release_ids": releases,
        "strict_acceptance_claimed": False,
        "candidate_or_profit_claim": False,
        "rfq_reads": 0,
        "network_reads": 0,
        "production_mutations": 0,
        "order_actions": 0,
        "input_manifest_sha256": hashlib.sha256(
            (run_dir / "INPUT_MANIFEST.json").read_bytes()
        ).hexdigest(),
        "results_sha256": hashlib.sha256(
            (run_dir / "RESULTS.json").read_bytes()
        ).hexdigest(),
        "report_sha256": hashlib.sha256(original_report).hexdigest(),
        "artifact_sha256sums_sha256": hashlib.sha256(sums_raw).hexdigest(),
        "artifacts": artifact_rows,
        "authority_binding": authority_binding,
        "authority_artifact_sha256s": authority_hashes,
    }
    (run_dir / "RUN_COMPLETE.json").write_bytes(worker._artifact_bytes(complete))

    adopted_path = run_dir / "ADOPTED_PLAN.md"
    adopted_raw = adopted_path.read_bytes()
    adopted_path.write_bytes(adopted_raw + b"tampered")
    with pytest.raises(worker.ResearchJobWorkerError, match="authority artifact hash"):
        worker.finalize_deep03_job_output(
            inbox_root=remote, job_id=job_id, run_dir=run_dir
        )
    adopted_path.write_bytes(adopted_raw)
    assert get_job(remote, job_id)["status"]["state"] == "RUNNING"

    finalized = worker.finalize_deep03_job_output(
        inbox_root=remote, job_id=job_id, run_dir=run_dir
    )
    assert finalized["state"] == "COMPLETE"
    assert finalized["research_started_by_finalizer"] is False
    assert get_job(remote, job_id)["status"]["state"] == "COMPLETE"
    remote_results = json.loads(
        (remote / "jobs" / job_id / "OUTPUT" / "RESULTS.json").read_text()
    )
    assert remote_results["job_id"] == job_id
    assert remote_results["source_results"] == deep_results
    assert remote_results["execution_provenance"]["source_run_complete_sha256"] == (
        hashlib.sha256((run_dir / "RUN_COMPLETE.json").read_bytes()).hexdigest()
    )

    archive = control.export_outputs(remote, job_id)
    key = _key(tmp_path)

    def export_runner(command, **kwargs):
        assert "export" in command and kwargs["shell"] is False
        return subprocess.CompletedProcess(command, 0, archive, b"")

    pulled = pull_job_outputs(local, job_id, ssh_key=key, runner=export_runner)
    assert pulled["remote_state"] == "COMPLETE"
    assert get_job(local, job_id)["report_available"] is True
    original_validator = inbox_module._validated_output_tree
    inbox_module._validated_output_tree = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("history bytes were rehashed")
    )
    try:
        assert get_job(local, job_id)["report_available"] is True
    finally:
        inbox_module._validated_output_tree = original_validator
    assert read_report(local, job_id) == original_report
    handler = dashboard_server.make_handler(
        str(tmp_path / "metrics.prom"),
        False,
        research_inbox_root=local,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = "http://127.0.0.1:%d" % server.server_port
        with urllib.request.urlopen(base + "/api/research/jobs", timeout=5) as response:
            listed = json.loads(response.read())
        assert listed["jobs"][0]["job_id"] == job_id
        assert listed["jobs"][0]["report_available"] is True
        with urllib.request.urlopen(
            base + "/api/research/jobs/%s/report" % job_id, timeout=5
        ) as response:
            assert response.read() == original_report
            assert response.headers["Content-Security-Policy"].startswith("sandbox;")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    # A changed report invalidates the cached dashboard affordance.
    report_path = local / "jobs" / job_id / "REPORT" / "index.html"
    report_path.write_bytes(report_path.read_bytes() + b"tampered")
    assert get_job(local, job_id)["report_available"] is False


def test_real_dispatch_start_worker_ready_export_pull_flow(tmp_path):
    """Exercise both protocol ends with the real planning worker, no network."""
    control = _control_module()
    worker = _worker_module()
    worker.build_catalog = lambda _cache: _catalog()
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    remote.mkdir()
    run_root = tmp_path / "run"
    job = _local_job(local)
    job_id = job["job_id"]
    key = _key(tmp_path)

    class InProcessTransport:
        def __init__(self):
            self.commands = []

        @staticmethod
        def _argument(command, name):
            return command[command.index(name) + 1]

        def _systemctl(self, command, **kwargs):
            assert command == [
                "/usr/bin/systemctl",
                "start",
                "w09-research-inbox-worker@%s.service" % job_id,
            ]
            assert kwargs["shell"] is False
            control.verify_start_arm(remote, run_root, job_id)
            result = worker.plan_job(
                inbox_root=remote,
                cache_root=tmp_path / "cache",
                job_id=job_id,
            )
            assert result["state"] == "READY"
            return subprocess.CompletedProcess(command, 0, b"", b"")

        def __call__(self, command, **kwargs):
            assert kwargs["shell"] is False
            self.commands.append(command)
            if command[0] == "scp":
                separator = command.index("--")
                sources = command[separator + 1 : -1]
                incoming = remote / ".incoming" / job_id
                assert tuple(Path(path).name for path in sources) == control.CONTROL_FILES
                for source in sources:
                    shutil.copyfile(source, incoming / Path(source).name)
                return subprocess.CompletedProcess(command, 0, b"", b"")
            assert command[0] == "ssh"
            helper = command.index(str(control.CONTROL_WRAPPER))
            action = command[helper + 1]
            remote_job_id = self._argument(command, "--job-id")
            assert remote_job_id == job_id
            if action == "receive-prepare":
                value = control.prepare_receive(
                    remote, job_id, self._argument(command, "--bundle-sha256")
                )
                stdout = json.dumps(value).encode()
            elif action == "receive-commit":
                value = control.commit_receive(
                    remote, job_id, self._argument(command, "--bundle-sha256")
                )
                stdout = json.dumps(value).encode()
            elif action == "receive-abort":
                value = control.abort_receive(
                    remote, job_id, self._argument(command, "--bundle-sha256")
                )
                stdout = json.dumps(value).encode()
            elif action == "start":
                value = control.start_committed_job(
                    remote,
                    run_root,
                    job_id,
                    self._argument(command, "--bundle-sha256"),
                    runner=self._systemctl,
                    require_root=False,
                )
                stdout = json.dumps(value).encode()
            elif action == "export":
                stdout = control.export_outputs(remote, job_id)
            else:  # pragma: no cover - fixed client action set
                raise AssertionError(action)
            return subprocess.CompletedProcess(command, 0, stdout, b"")

    transport = InProcessTransport()
    dispatched = dispatch_job(local, job_id, ssh_key=key, runner=transport)
    assert dispatched["state"] == "COMMITTED_NOT_STARTED"
    assert get_job(remote, job_id)["status"]["state"] == "QUEUED"

    started = start_job(
        local,
        job_id,
        bundle_sha256=dispatched["bundle_sha256"],
        ssh_key=key,
        runner=transport,
    )
    assert started["state"] == "START_REQUEST_ACCEPTED"
    assert get_job(remote, job_id)["status"]["state"] == "READY"
    assert (remote / "jobs" / job_id / "PLANNING" / "PLANNING_RECEIPT.json").is_file()

    pulled = pull_job_outputs(local, job_id, ssh_key=key, runner=transport)
    assert pulled["remote_state"] == "READY"
    assert pulled["data_files_transferred"] == 0
    assert get_job(local, job_id)["status"]["state"] == "READY"
    state = json.loads((remote / ".control" / (job_id + ".json")).read_text())
    assert state["state"] == "WORKER_INVOKED"
    assert state["last_status_state"] == "READY"
    assert state["initial_status_sha256"] != state["last_status_sha256"]
    replayed_pull = pull_job_outputs(local, job_id, ssh_key=key, runner=transport)
    assert replayed_pull["remote_state"] == "READY"
    assert get_job(local, job_id)["status"]["state"] == "READY"


def test_commit_crash_replay_recovers_but_planned_job_cannot_be_overwritten(tmp_path):
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
        (incoming / name).chmod(0o440)
    incoming.chmod(0o750)
    # Simulate process loss after atomic rename but before COMMITTED state write.
    incoming.rename(remote / "jobs" / job_id)

    control.prepare_receive(remote, job_id, bundle)
    retry_incoming = remote / ".incoming" / job_id
    for name in control.CONTROL_FILES:
        shutil.copyfile(source / name, retry_incoming / name)
    replay = control.commit_receive(remote, job_id, bundle)
    assert replay["idempotent"] is True
    final = remote / "jobs" / job_id
    immutable_before = {
        name: hashlib.sha256((final / name).read_bytes()).hexdigest()
        for name in ("PLAN.md", "JOB_SPEC.json", "REQUEST.json")
    }

    def runner(command, **kwargs):
        control.verify_start_arm(remote, tmp_path / "run", job_id)
        update_status(remote, job_id, state="PREFLIGHT", message="planning")
        update_status(remote, job_id, state="READY", message="planned")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    control.start_committed_job(
        remote,
        tmp_path / "run",
        job_id,
        bundle,
        runner=runner,
        require_root=False,
    )
    with pytest.raises(control.ResearchInboxControlError, match="cannot be prepared"):
        control.prepare_receive(remote, job_id, bundle)
    with pytest.raises(control.ResearchInboxControlError, match="pristine initial"):
        control.start_committed_job(
            remote,
            tmp_path / "run-2",
            job_id,
            bundle,
            runner=runner,
            require_root=False,
        )
    assert not (remote / ".incoming" / job_id).exists()
    assert immutable_before == {
        name: hashlib.sha256((final / name).read_bytes()).hexdigest()
        for name in ("PLAN.md", "JOB_SPEC.json", "REQUEST.json")
    }

    initial_status = (source / "STATUS.json").read_bytes()
    status_stage = final / ".STATUS.regression"
    status_stage.write_bytes(initial_status)
    status_stage.replace(final / "STATUS.json")
    with pytest.raises(control.ResearchInboxControlError, match="regressed"):
        control.export_outputs(remote, job_id)


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
