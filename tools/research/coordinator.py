#!/usr/bin/env python3
"""Durable Mac-side coordinator for automatic W09 Research Inbox planning.

The coordinator moves only the four small job control files through the fixed
W09 transport.  It never transfers market data and never interprets plan text.
Its persisted intent/receipts let a dashboard restart resume at pull/start
boundaries without blindly submitting an already-started job again.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable

from . import inbox
from . import w09_dispatch


SCHEMA_STATUS = "research-w09-coordinator-status-v1"
MAX_RECEIPT_BYTES = 8 * 1024 * 1024


class CoordinatorError(RuntimeError):
    """A queued job could not advance through the fixed W09 control plane."""


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _job_dir(root: Path, job_id: str) -> Path:
    job = inbox.get_job(root, job_id)
    path = Path(root).expanduser().resolve() / "jobs" / job["job_id"]
    if path.is_symlink() or not path.is_dir():
        raise CoordinatorError("research job directory is unsafe")
    return path


def _write_atomic(path: Path, value: dict[str, Any], *, immutable: bool) -> None:
    payload = _canonical(value)
    if len(payload) > MAX_RECEIPT_BYTES:
        raise CoordinatorError("coordinator receipt exceeds its size limit")
    if path.is_symlink():
        raise CoordinatorError("coordinator receipt destination is a symlink")
    if immutable and path.exists():
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise CoordinatorError("coordinator receipt is unreadable") from exc
        if existing != payload:
            raise CoordinatorError("immutable coordinator receipt conflicts")
        return
    temporary = path.parent / (".%s.tmp.%d.%d" % (
        path.name, os.getpid(), threading.get_ident()
    ))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o640)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)
    try:
        if immutable and path.exists():
            if path.read_bytes() != payload:
                raise CoordinatorError("immutable coordinator receipt conflicts")
            return
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_optional(path: Path) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RECEIPT_BYTES:
        raise CoordinatorError("coordinator receipt is unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise CoordinatorError("coordinator receipt is unreadable") from exc
    if not isinstance(value, dict):
        raise CoordinatorError("coordinator receipt root is not an object")
    return value


def _coordination_status(
    job_dir: Path,
    job_id: str,
    *,
    state: str,
    message: str,
    retryable: bool = False,
    attempt: int = 1,
    remote_state: str | None = None,
) -> None:
    value: dict[str, Any] = {
        "schema_version": SCHEMA_STATUS,
        "job_id": job_id,
        "state": state,
        "message": message,
        "updated_at_utc": _utc(),
        "retryable": bool(retryable),
        "attempt": int(attempt),
        "market_data_files_transferred": 0,
        "data_access": "W09_INSTANCE_PROFILE_EXACT_VERSION_READONLY",
    }
    if remote_state is not None:
        value["remote_state"] = remote_state
    _write_atomic(job_dir / "W09_COORDINATOR_STATUS.json", value, immutable=False)


def process_job(
    inbox_root: Path,
    job_id: str,
    *,
    ssh_key: Path = w09_dispatch.DEFAULT_SSH_KEY,
    attempt: int = 1,
    dispatch: Callable[..., dict[str, Any]] = w09_dispatch.dispatch_job,
    start: Callable[..., dict[str, Any]] = w09_dispatch.start_job,
    pull: Callable[..., dict[str, Any]] = w09_dispatch.pull_job_outputs,
) -> dict[str, Any]:
    """Advance one QUEUED job through dispatch, planning, and status pull."""
    root = Path(inbox_root).expanduser().resolve()
    job = inbox.get_job(root, job_id)
    job_dir = _job_dir(root, job_id)
    if job["status"].get("state") != "QUEUED":
        return {"job_id": job_id, "state": "NOT_QUEUED"}
    if job["request"].get("automatic_execution_requested") is not True:
        return {"job_id": job_id, "state": "MANUAL_ONLY"}

    dispatch_path = job_dir / "W09_DISPATCH_RECEIPT.json"
    start_intent_path = job_dir / "W09_START_INTENT.json"
    start_path = job_dir / "W09_START_RECEIPT.json"
    dispatch_receipt = _read_optional(dispatch_path)
    try:
        if dispatch_receipt is None:
            _coordination_status(
                job_dir, job_id, state="DISPATCHING", message="Sending immutable controls to W09.",
                attempt=attempt,
            )
            dispatch_receipt = dispatch(root, job_id, ssh_key=ssh_key)
            if dispatch_receipt.get("job_id") != job_id:
                raise CoordinatorError("dispatch receipt job id mismatch")
            _write_atomic(dispatch_path, dispatch_receipt, immutable=True)
        bundle_sha = dispatch_receipt.get("bundle_sha256")
        if not isinstance(bundle_sha, str) or len(bundle_sha) != 64:
            raise CoordinatorError("dispatch receipt has no exact bundle SHA-256")

        # Pull first on recovery.  If a prior start finished between receipt
        # writes, this observes READY/terminal state without replaying start.
        if start_intent_path.exists() or start_path.exists():
            pulled = pull(root, job_id, ssh_key=ssh_key)
            remote_state = pulled.get("remote_state")
            if remote_state != "QUEUED":
                _coordination_status(
                    job_dir, job_id, state="REMOTE_STATUS_SYNCED",
                    message="W09 planning status returned to the Research Inbox.",
                    attempt=attempt, remote_state=str(remote_state),
                )
                return pulled

        if _read_optional(start_path) is None:
            existing_intent = _read_optional(start_intent_path)
            if existing_intent is None:
                intent = {
                    "schema_version": "research-w09-start-intent-v1",
                    "job_id": job_id,
                    "bundle_sha256": bundle_sha,
                    "created_at_utc": _utc(),
                }
                _write_atomic(start_intent_path, intent, immutable=True)
            elif (
                existing_intent.get("schema_version") != "research-w09-start-intent-v1"
                or existing_intent.get("job_id") != job_id
                or existing_intent.get("bundle_sha256") != bundle_sha
            ):
                raise CoordinatorError("persisted start intent binding mismatch")
            _coordination_status(
                job_dir, job_id, state="PLANNING_ON_W09",
                message="W09 is selecting exact available data for the registered method.",
                attempt=attempt,
            )
            start_receipt = start(
                root, job_id, bundle_sha256=bundle_sha, ssh_key=ssh_key
            )
            if start_receipt.get("job_id") != job_id:
                raise CoordinatorError("start receipt job id mismatch")
            _write_atomic(start_path, start_receipt, immutable=True)

        pulled = pull(root, job_id, ssh_key=ssh_key)
        remote_state = pulled.get("remote_state")
        _coordination_status(
            job_dir, job_id, state="REMOTE_STATUS_SYNCED",
            message="W09 planning status returned to the Research Inbox.",
            attempt=attempt, remote_state=str(remote_state),
        )
        return pulled
    except Exception as exc:
        _coordination_status(
            job_dir, job_id, state="RETRYABLE_ERROR",
            message="W09 coordination will retry: %s" % type(exc).__name__,
            retryable=True, attempt=attempt,
        )
        if isinstance(exc, CoordinatorError):
            raise
        raise CoordinatorError("W09 coordination failed closed") from exc


class ResearchW09Coordinator:
    """Small daemon thread that resumes automatic QUEUED jobs safely."""

    def __init__(
        self,
        inbox_root: Path,
        *,
        ssh_key: Path = w09_dispatch.DEFAULT_SSH_KEY,
        poll_seconds: float = 30.0,
    ) -> None:
        self.inbox_root = Path(inbox_root).expanduser().resolve()
        self.ssh_key = Path(ssh_key).expanduser()
        self.poll_seconds = max(2.0, float(poll_seconds))
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._attempts: dict[str, int] = {}
        self._next_attempt: dict[str, float] = {}

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="research-w09-coordinator", daemon=True
        )
        self._thread.start()

    def submit(self, _job_id: str) -> None:
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout))

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                jobs = inbox.list_jobs(self.inbox_root, limit=1000)
            except inbox.InboxError:
                jobs = []
            for job in reversed(jobs):
                if self._stop.is_set():
                    break
                job_id = job["job_id"]
                if (
                    job["status"].get("state") != "QUEUED"
                    or job["request"].get("automatic_execution_requested") is not True
                    or now < self._next_attempt.get(job_id, 0.0)
                ):
                    continue
                attempt = self._attempts.get(job_id, 0) + 1
                try:
                    process_job(
                        self.inbox_root,
                        job_id,
                        ssh_key=self.ssh_key,
                        attempt=attempt,
                    )
                    self._attempts.pop(job_id, None)
                    self._next_attempt.pop(job_id, None)
                except CoordinatorError:
                    self._attempts[job_id] = attempt
                    self._next_attempt[job_id] = time.monotonic() + min(
                        300.0, 5.0 * (2 ** min(attempt - 1, 6))
                    )
            self._wake.wait(self.poll_seconds)
            self._wake.clear()


__all__ = [
    "CoordinatorError",
    "ResearchW09Coordinator",
    "process_job",
]
