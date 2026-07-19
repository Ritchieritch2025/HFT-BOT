#!/usr/bin/env python3
"""Offline recovery contracts for the Mac W09 research coordinator."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from research.coordinator import CoordinatorError, process_job  # noqa: E402
from research.inbox import create_job, get_job, update_status  # noqa: E402
from research.plan_contract import compile_plan  # noqa: E402


PLAN = """---
plugin: deep03
data:
  required: [L1, L2, TRADES, MARKET_GRAPH]
  forbidden: [RFQ]
---
# Coordinator test
"""


def _job(root: Path):
    return create_job(
        root,
        filename="PLAN.md",
        plan_text=PLAN,
        job_spec=compile_plan(PLAN),
        automatic_execution_requested=True,
    )


def _ready_pull(root: Path, job_id: str, **_kwargs):
    current = get_job(root, job_id)["status"]["state"]
    if current == "QUEUED":
        update_status(root, job_id, state="PREFLIGHT", message="planning")
        update_status(root, job_id, state="READY", message="authority required")
    return {"job_id": job_id, "remote_state": "READY", "data_files_transferred": 0}


def test_coordinator_dispatches_plans_and_pulls_remote_ready_status(tmp_path):
    job = _job(tmp_path)
    calls = []

    def dispatch(root, job_id, **_kwargs):
        calls.append("dispatch")
        return {"job_id": job_id, "bundle_sha256": "a" * 64}

    def start(root, job_id, **_kwargs):
        calls.append("start")
        return {"job_id": job_id, "state": "START_REQUEST_ACCEPTED"}

    result = process_job(
        tmp_path,
        job["job_id"],
        dispatch=dispatch,
        start=start,
        pull=lambda root, job_id, **kwargs: (
            calls.append("pull") or _ready_pull(root, job_id, **kwargs)
        ),
    )

    assert calls == ["dispatch", "start", "pull"]
    assert result["remote_state"] == "READY"
    current = get_job(tmp_path, job["job_id"])
    assert current["status"]["state"] == "READY"
    assert current["coordination"]["state"] == "REMOTE_STATUS_SYNCED"
    job_dir = tmp_path / "jobs" / job["job_id"]
    assert json.loads((job_dir / "W09_DISPATCH_RECEIPT.json").read_text())[
        "bundle_sha256"
    ] == "a" * 64
    assert (job_dir / "W09_START_INTENT.json").is_file()
    assert (job_dir / "W09_START_RECEIPT.json").is_file()


def test_recovery_pulls_before_start_and_reuses_existing_intent(tmp_path):
    job = _job(tmp_path)
    job_dir = tmp_path / "jobs" / job["job_id"]
    (job_dir / "W09_DISPATCH_RECEIPT.json").write_text(
        json.dumps({"job_id": job["job_id"], "bundle_sha256": "b" * 64},
                   sort_keys=True, separators=(",", ":")) + "\n"
    )
    (job_dir / "W09_START_INTENT.json").write_text(
        json.dumps({
            "schema_version": "research-w09-start-intent-v1",
            "job_id": job["job_id"],
            "bundle_sha256": "b" * 64,
            "created_at_utc": "2026-07-19T00:00:00Z",
        }, sort_keys=True, separators=(",", ":")) + "\n"
    )
    calls = []

    def pull(root, job_id, **_kwargs):
        calls.append("pull")
        if calls == ["pull"]:
            return {"job_id": job_id, "remote_state": "QUEUED"}
        return _ready_pull(root, job_id)

    process_job(
        tmp_path,
        job["job_id"],
        dispatch=lambda *_args, **_kwargs: pytest.fail("dispatch replayed"),
        start=lambda _root, job_id, **_kwargs: (
            calls.append("start") or {"job_id": job_id}
        ),
        pull=pull,
    )

    assert calls == ["pull", "start", "pull"]
    assert get_job(tmp_path, job["job_id"])["status"]["state"] == "READY"


def test_retryable_failure_keeps_job_queued_and_exposes_safe_status(tmp_path):
    job = _job(tmp_path)

    with pytest.raises(CoordinatorError, match="failed closed"):
        process_job(
            tmp_path,
            job["job_id"],
            dispatch=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("secret detail")),
        )

    current = get_job(tmp_path, job["job_id"])
    assert current["status"]["state"] == "QUEUED"
    assert current["coordination"]["state"] == "RETRYABLE_ERROR"
    assert "secret detail" not in current["coordination"]["message"]


def test_nonqueued_job_is_not_dispatched(tmp_path):
    job = _job(tmp_path)
    update_status(tmp_path, job["job_id"], state="BLOCKED", message="no data")
    result = process_job(
        tmp_path,
        job["job_id"],
        dispatch=lambda *_args, **_kwargs: pytest.fail("dispatched blocked job"),
    )
    assert result["state"] == "NOT_QUEUED"
