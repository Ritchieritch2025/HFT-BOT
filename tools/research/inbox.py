#!/usr/bin/env python3
"""Immutable local spool for general Research Inbox jobs.

The inbox stores plans and canonical job specs.  It never executes plan text,
opens network connections, or interprets Markdown as Python/SQL.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import threading
from typing import Any


SCHEMA_REQUEST = "research-job-request-v1"
SCHEMA_STATUS = "research-job-status-v1"
MAX_PLAN_BYTES = 2 * 1024 * 1024
JOB_ID_RE = re.compile(r"^RJOB-\d{8}T\d{12}Z-[0-9a-f]{12}$")
TERMINAL_STATES = {"COMPLETE", "BLOCKED", "FAILED", "REFUSED"}
TRANSITIONS = {
    "NEEDS_METHOD": {"QUEUED", "BLOCKED", "REFUSED"},
    "QUEUED": {"PREFLIGHT", "BLOCKED", "FAILED", "REFUSED"},
    "PREFLIGHT": {"READY", "BLOCKED", "FAILED", "REFUSED"},
    "READY": {"RUNNING", "BLOCKED", "FAILED", "REFUSED"},
    "RUNNING": {"COMPLETE", "BLOCKED", "FAILED", "REFUSED"},
}


class InboxError(ValueError):
    """A job spool request violated the local immutable contract."""


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, payload: bytes, *, exclusive: bool = False) -> None:
    path = Path(path)
    tmp = path.parent / (
        ".%s.tmp.%d.%d" % (path.name, os.getpid(), threading.get_ident())
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(tmp, flags, 0o640)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)
    try:
        if exclusive and path.exists():
            raise InboxError("artifact already exists: %s" % path.name)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise InboxError("%s is missing or unsafe" % label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise InboxError("%s is unreadable" % label) from exc
    if not isinstance(value, dict):
        raise InboxError("%s root is not an object" % label)
    return value


def _root(path: Path) -> Path:
    path = Path(path).expanduser()
    if path.is_symlink():
        raise InboxError("inbox root cannot be a symlink")
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o750)
    jobs = path / "jobs"
    jobs.mkdir(exist_ok=True, mode=0o750)
    if jobs.is_symlink():
        raise InboxError("inbox jobs root cannot be a symlink")
    return path


def _utc(value: dt.datetime | None = None) -> tuple[dt.datetime, str]:
    now = value or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None or now.utcoffset() != dt.timedelta(0):
        raise InboxError("job timestamp must be UTC")
    now = now.astimezone(dt.timezone.utc)
    return now, now.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _job_id(now: dt.datetime, plan_sha256: str) -> str:
    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    return "RJOB-%s-%s" % (stamp, plan_sha256[:12])


def create_job(
    inbox_root: Path,
    *,
    filename: str,
    plan_text: str,
    job_spec: dict[str, Any],
    automatic_execution_requested: bool = True,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Atomically enqueue one compiled plan without executing it."""
    root = _root(inbox_root)
    if not isinstance(plan_text, str) or not plan_text:
        raise InboxError("plan text is empty")
    plan_raw = plan_text.encode("utf-8")
    if len(plan_raw) > MAX_PLAN_BYTES:
        raise InboxError("plan exceeds 2 MiB")
    if not isinstance(job_spec, dict):
        raise InboxError("job spec is not an object")
    if job_spec.get("schema_version") != "research-job-spec-v1":
        raise InboxError("job spec schema is unsupported")
    plan_sha = job_spec.get("plan_sha256")
    plan_bytes = job_spec.get("plan_bytes")
    import hashlib

    if plan_sha != hashlib.sha256(plan_raw).hexdigest() or plan_bytes != len(plan_raw):
        raise InboxError("job spec does not bind the exact plan bytes")
    spec_state = job_spec.get("state")
    if spec_state not in {"READY", "NEEDS_METHOD"}:
        raise InboxError("job spec state must be READY or NEEDS_METHOD")

    instant, created_at = _utc(now)
    job_id = _job_id(instant, plan_sha)
    if JOB_ID_RE.fullmatch(job_id) is None:
        raise InboxError("generated job id is invalid")
    final = root / "jobs" / job_id
    stage = root / "jobs" / (".%s.preparing.%d" % (job_id, os.getpid()))
    if final.exists() or stage.exists():
        raise InboxError("job id collision")
    stage.mkdir(mode=0o750)
    try:
        safe_name = Path(filename or "PLAN.md").name
        request = {
            "schema_version": SCHEMA_REQUEST,
            "job_id": job_id,
            "created_at_utc": created_at,
            "source_filename": safe_name,
            "plan_sha256": plan_sha,
            "plan_bytes": len(plan_raw),
            "automatic_execution_requested": bool(automatic_execution_requested),
            "execution_target": "W09_ISOLATED_RESEARCH",
            "data_access": "ZERO_COPY_EXACT_VERSION_READONLY",
            "arbitrary_plan_code_execution": False,
        }
        initial_state = "QUEUED" if spec_state == "READY" else "NEEDS_METHOD"
        status = {
            "schema_version": SCHEMA_STATUS,
            "job_id": job_id,
            "state": initial_state,
            "updated_at_utc": created_at,
            "research_execution_started": False,
            "report_ready": False,
            "next_action": (
                "AUTOMATIC_DATA_PREFLIGHT"
                if initial_state == "QUEUED"
                else "REGISTER_HASH_PINNED_METHOD_PLUGIN"
            ),
            "message": (
                "Queued for automatic data preflight."
                if initial_state == "QUEUED"
                else "Plan accepted; no registered method plugin matches it yet."
            ),
        }
        _atomic_write(stage / "PLAN.md", plan_raw, exclusive=True)
        _atomic_write(stage / "JOB_SPEC.json", _canonical_json_bytes(job_spec), exclusive=True)
        _atomic_write(stage / "REQUEST.json", _canonical_json_bytes(request), exclusive=True)
        _atomic_write(stage / "STATUS.json", _canonical_json_bytes(status), exclusive=True)
        os.replace(stage, final)
        dir_fd = os.open(final.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return get_job(root, job_id)


def get_job(inbox_root: Path, job_id: str) -> dict[str, Any]:
    root = _root(inbox_root)
    if not isinstance(job_id, str) or JOB_ID_RE.fullmatch(job_id) is None:
        raise InboxError("invalid job id")
    path = root / "jobs" / job_id
    if not path.is_dir() or path.is_symlink():
        raise InboxError("job not found")
    request = _read_json(path / "REQUEST.json", "job request")
    status = _read_json(path / "STATUS.json", "job status")
    spec = _read_json(path / "JOB_SPEC.json", "job spec")
    return {
        "job_id": job_id,
        "request": request,
        "status": status,
        "spec": spec,
        "report_available": (
            (path / "REPORT" / "index.html").is_file()
            and not (path / "REPORT" / "index.html").is_symlink()
        ),
    }


def list_jobs(inbox_root: Path, *, limit: int = 200) -> list[dict[str, Any]]:
    root = _root(inbox_root)
    limit = max(1, min(int(limit), 1000))
    rows = []
    for path in sorted((root / "jobs").iterdir(), reverse=True):
        if len(rows) >= limit:
            break
        if path.is_symlink() or not path.is_dir() or JOB_ID_RE.fullmatch(path.name) is None:
            continue
        try:
            rows.append(get_job(root, path.name))
        except InboxError:
            continue
    return rows


def update_status(
    inbox_root: Path,
    job_id: str,
    *,
    state: str,
    message: str,
    details: dict[str, Any] | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Advance a worker-owned job through the fixed state machine."""
    root = _root(inbox_root)
    job = get_job(root, job_id)
    current = job["status"].get("state")
    if state not in TRANSITIONS.get(current, set()):
        raise InboxError("invalid job transition: %s -> %s" % (current, state))
    _, updated_at = _utc(now)
    status = dict(job["status"])
    status.update(
        {
            "state": state,
            "updated_at_utc": updated_at,
            "message": str(message),
            "research_execution_started": state in {"RUNNING", "COMPLETE"},
            "report_ready": state == "COMPLETE",
            "next_action": None if state in TERMINAL_STATES else state,
        }
    )
    if details is not None:
        status["details"] = details
    _atomic_write(root / "jobs" / job_id / "STATUS.json", _canonical_json_bytes(status))
    return get_job(root, job_id)
