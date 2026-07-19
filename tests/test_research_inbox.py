#!/usr/bin/env python3
"""Contracts for the immutable Research Inbox spool."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from research.inbox import (  # noqa: E402
    InboxError,
    create_job,
    get_job,
    list_jobs,
    update_status,
)


def _spec(text: str, *, state: str = "READY") -> dict:
    raw = text.encode()
    return {
        "schema_version": "research-job-spec-v1",
        "state": state,
        "plan_sha256": hashlib.sha256(raw).hexdigest(),
        "plan_bytes": len(raw),
        "title": "spread experiment",
        "plugin_id": "deep03" if state == "READY" else None,
        "data_requirements": {"required": ["L1"]},
        "execution_class": "READONLY_EXPLORATORY",
    }


def test_create_list_and_advance_exact_plan(tmp_path):
    text = "# Test spread response\n"
    now = dt.datetime(2026, 7, 18, 23, 59, 1, 123456, tzinfo=dt.timezone.utc)
    job = create_job(
        tmp_path,
        filename="../../PLAN.md",
        plan_text=text,
        job_spec=_spec(text),
        now=now,
    )
    job_id = job["job_id"]
    assert job_id == "RJOB-20260718T235901123456Z-" + hashlib.sha256(text.encode()).hexdigest()[:12]
    assert job["status"]["state"] == "QUEUED"
    assert job["request"]["source_filename"] == "PLAN.md"
    assert job["request"]["data_access"] == "ZERO_COPY_EXACT_VERSION_READONLY"
    assert (tmp_path / "jobs" / job_id / "PLAN.md").read_text() == text
    assert list_jobs(tmp_path)[0]["job_id"] == job_id

    job = update_status(tmp_path, job_id, state="PREFLIGHT", message="checking")
    assert job["status"]["state"] == "PREFLIGHT"
    assert get_job(tmp_path, job_id)["status"]["message"] == "checking"


def test_unknown_method_is_accepted_but_never_executed(tmp_path):
    text = "# Novel experiment\n"
    job = create_job(
        tmp_path,
        filename="idea.md",
        plan_text=text,
        job_spec=_spec(text, state="NEEDS_METHOD"),
    )
    assert job["status"]["state"] == "NEEDS_METHOD"
    assert job["status"]["research_execution_started"] is False
    assert "REGISTER_HASH_PINNED_METHOD_PLUGIN" == job["status"]["next_action"]


def test_refuses_spec_drift_bad_ids_and_state_replay(tmp_path):
    text = "# Exact bytes\n"
    spec = _spec(text)
    spec["plan_sha256"] = "0" * 64
    with pytest.raises(InboxError, match="exact plan bytes"):
        create_job(tmp_path, filename="plan.md", plan_text=text, job_spec=spec)
    with pytest.raises(InboxError, match="invalid job id"):
        get_job(tmp_path, "../../etc/passwd")

    job = create_job(
        tmp_path,
        filename="plan.md",
        plan_text=text,
        job_spec=_spec(text),
    )
    with pytest.raises(InboxError, match="invalid job transition"):
        update_status(tmp_path, job["job_id"], state="COMPLETE", message="skip")


def test_running_job_cannot_claim_complete_without_sealed_output(tmp_path):
    text = "# Completion guard\n"
    job = create_job(
        tmp_path, filename="plan.md", plan_text=text, job_spec=_spec(text)
    )
    for state in ("PREFLIGHT", "READY", "RUNNING"):
        update_status(tmp_path, job["job_id"], state=state, message=state)
    with pytest.raises(InboxError, match="sealed, hash-validated"):
        update_status(tmp_path, job["job_id"], state="COMPLETE", message="false pass")
    assert get_job(tmp_path, job["job_id"])["status"]["state"] == "RUNNING"


def test_corrupt_or_linked_jobs_are_not_listed(tmp_path):
    (tmp_path / "jobs").mkdir(parents=True)
    bad = tmp_path / "jobs" / "RJOB-20260718T235901123456Z-aaaaaaaaaaaa"
    bad.mkdir()
    (bad / "STATUS.json").write_text("not json")
    assert list_jobs(tmp_path) == []

    external = tmp_path / "external"
    external.mkdir()
    linked = tmp_path / "jobs" / "RJOB-20260718T235902123456Z-bbbbbbbbbbbb"
    linked.symlink_to(external, target_is_directory=True)
    assert list_jobs(tmp_path) == []


def test_linked_root_or_report_is_never_followed(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(actual, target_is_directory=True)
    with pytest.raises(InboxError, match="root cannot be a symlink"):
        list_jobs(linked_root)

    text = "# Safe report boundary\n"
    job = create_job(
        actual,
        filename="plan.md",
        plan_text=text,
        job_spec=_spec(text),
    )
    report = actual / "jobs" / job["job_id"] / "REPORT"
    report.mkdir()
    outside = tmp_path / "outside.html"
    outside.write_text("not a job report")
    (report / "index.html").symlink_to(outside)
    assert get_job(actual, job["job_id"])["report_available"] is False
