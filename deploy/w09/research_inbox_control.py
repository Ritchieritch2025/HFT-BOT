#!/usr/bin/env python3
"""Fixed W09-side control protocol for the generic Research Inbox.

The SSH-facing helper accepts only validated Research Inbox job IDs and four
small control files.  It atomically renames a verified incoming directory into
``/srv/w09-research/inbox/jobs``.  Research data is never uploaded here; the
planning worker sees only W09's already verified exact-version cache.

``start`` is deliberately separate from receive/commit.  The installed CLI
re-enters this program through a narrowly scoped sudo rule, writes a short
root-owned arm in ``/run``, and starts one fixed systemd template instance.
The template's ExecCondition verifies the arm, so a direct systemctl call
without the explicit helper action fails closed.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
from typing import Any, Callable, Iterator, Mapping, Sequence


PROTOCOL = "research-w09-dispatch-interface-v1"
JOB_ID_RE = re.compile(r"^RJOB-\d{8}T\d{12}Z-[0-9a-f]{12}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REPORT_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CONTROL_FILES = ("PLAN.md", "JOB_SPEC.json", "REQUEST.json", "STATUS.json")
CONTROL_LIMITS = {
    "PLAN.md": 2 * 1024 * 1024,
    "JOB_SPEC.json": 1024 * 1024,
    "REQUEST.json": 1024 * 1024,
    "STATUS.json": 1024 * 1024,
}
SCHEMA_BUNDLE = "research-w09-control-bundle-v1"
SCHEMA_STATE = "research-w09-remote-control-state-v1"
SCHEMA_ARM = "research-w09-explicit-start-arm-v1"
DATA_PLANE = "W09_INSTANCE_PROFILE_EXACT_VERSION_READONLY"
EXPORT_FORMAT = "research-w09-output-tar-v2"
ARTIFACT_SET = "STATUS_RESULTS_REPORT_RECEIPT"
OUTPUT_SCHEMA = "research-job-output-receipt-v1"
RESULTS_SCHEMA = "research-results-v1"
REPORT_RECEIPT_SCHEMA = "research-report-receipt-v1"
INBOX_ROOT = Path("/srv/w09-research/inbox")
RUN_ARM_ROOT = Path("/run/w09-research-inbox")
WORKER_UNIT_TEMPLATE = "w09-research-inbox-worker@.service"
CONTROL_WRAPPER = Path("/usr/local/libexec/w09-research-inbox-control")
SYSTEMCTL = "/usr/bin/systemctl"
MAX_EXPORT_BYTES = 64 * 1024 * 1024
MAX_EXPORT_FILE_BYTES = 32 * 1024 * 1024
MAX_EXPORT_FILES = 256
MAX_REPORT_DEPTH = 8
ARM_MAX_AGE_SECONDS = 60
REMOTE_LIFECYCLE_STATES = {
    "PREPARED",
    "ABORTED",
    "COMMITTED",
    "START_ARMED",
    "WORKER_INVOKED",
    "START_FAILED",
}
JOB_STATUS_RANK = {
    "QUEUED": 1,
    "PREFLIGHT": 2,
    "READY": 3,
    "RUNNING": 4,
    "COMPLETE": 5,
    "BLOCKED": 5,
    "FAILED": 5,
    "REFUSED": 5,
}
TERMINAL_JOB_STATES = {"COMPLETE", "BLOCKED", "FAILED", "REFUSED"}
STARTED_LIFECYCLE_STATES = {"START_ARMED", "WORKER_INVOKED", "START_FAILED"}

_PRIVATE_KEY_RE = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
)
_AWS_ACCESS_KEY_RE = re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_BEARER_RE = re.compile(rb"(?i)\bauthorization\s*[:=]\s*bearer\s+[A-Za-z0-9._~+/-]{12,}")
_SECRET_ASSIGNMENT_RE = re.compile(
    rb"(?i)(?:aws_secret_access_key|aws_session_token|secret_access_key|"
    rb"api[_-]?key|access[_-]?token|session[_-]?token|password|passwd)"
    rb"[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9/+_.~=-]{16,})"
)
_OPENAI_KEY_RE = re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")


class ResearchInboxControlError(RuntimeError):
    """A receive, start, or export request violated the fixed protocol."""


Runner = Callable[..., subprocess.CompletedProcess]


def _canonical(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ResearchInboxControlError("control value is not canonical JSON") from exc


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _job_id(value: str) -> str:
    if not isinstance(value, str) or JOB_ID_RE.fullmatch(value) is None:
        raise ResearchInboxControlError("invalid job id")
    return value


def _bundle_sha(value: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ResearchInboxControlError("invalid bundle SHA-256")
    return value


def _credential_like(payload: bytes) -> bool:
    return any(
        pattern.search(payload)
        for pattern in (
            _PRIVATE_KEY_RE,
            _AWS_ACCESS_KEY_RE,
            _BEARER_RE,
            _SECRET_ASSIGNMENT_RE,
            _OPENAI_KEY_RE,
        )
    )


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _json(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeError, ValueError) as exc:
        raise ResearchInboxControlError("%s is invalid JSON" % label) from exc
    if not isinstance(value, dict):
        raise ResearchInboxControlError("%s root is not an object" % label)
    return value


def _read_regular(path: Path, *, label: str, limit: int) -> bytes:
    if path.is_symlink():
        raise ResearchInboxControlError("%s is a symlink" % label)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ResearchInboxControlError("%s is missing or unsafe" % label) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size < 0 or before.st_size > limit:
            raise ResearchInboxControlError("%s is not a bounded regular file" % label)
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    stable_before = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    stable_after = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if stable_before != stable_after or len(payload) != before.st_size:
        raise ResearchInboxControlError("%s changed during read" % label)
    return payload


def _layout(root: Path) -> dict[str, Path]:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ResearchInboxControlError("fixed inbox root is missing or unsafe")
    paths = {
        "root": root,
        "jobs": root / "jobs",
        "incoming": root / ".incoming",
        "control": root / ".control",
        "locks": root / ".locks",
    }
    for label, path in paths.items():
        if label == "root":
            continue
        if path.is_symlink():
            raise ResearchInboxControlError("inbox %s root is a symlink" % label)
        path.mkdir(mode=0o750, exist_ok=True)
        if not path.is_dir():
            raise ResearchInboxControlError("inbox %s root is unsafe" % label)
    return paths


@contextmanager
def _lock(paths: Mapping[str, Path], job_id: str) -> Iterator[None]:
    lock_path = paths["locks"] / (job_id + ".lock")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags, 0o640)
    except OSError as exc:
        raise ResearchInboxControlError("job control lock is unsafe") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    payload = _canonical(value)
    temporary = path.parent / (".%s.tmp.%d" % (path.name, os.getpid()))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    # ``start`` runs through the root-only sudo boundary while receive/export
    # run as ubuntu.  State records contain no secret and live in a 0750
    # directory, so keep the replaced inode readable across that UID handoff;
    # ubuntu can atomically replace it because it owns the directory.
    fd = os.open(temporary, flags, 0o644)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)
    try:
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _state_path(paths: Mapping[str, Path], job_id: str) -> Path:
    return paths["control"] / (job_id + ".json")


def _read_state(paths: Mapping[str, Path], job_id: str) -> dict[str, Any] | None:
    path = _state_path(paths, job_id)
    if not path.exists() and not path.is_symlink():
        return None
    state = _json(_read_regular(path, label="dispatch state", limit=1024 * 1024), "dispatch state")
    if (
        state.get("schema_version") != SCHEMA_STATE
        or state.get("job_id") != job_id
        or state.get("state") not in REMOTE_LIFECYCLE_STATES
        or not isinstance(state.get("incoming_prepared"), bool)
    ):
        raise ResearchInboxControlError("dispatch state does not bind this job")
    if state.get("bundle_sha256") is not None:
        _bundle_sha(state["bundle_sha256"])
    if state["state"] in {"COMMITTED", *STARTED_LIFECYCLE_STATES}:
        immutable = state.get("immutable_sha256s")
        if (
            not isinstance(immutable, dict)
            or set(immutable) != {"PLAN.md", "JOB_SPEC.json", "REQUEST.json"}
            or any(
                not isinstance(value, str) or SHA256_RE.fullmatch(value) is None
                for value in immutable.values()
            )
            or not isinstance(state.get("initial_status_sha256"), str)
            or SHA256_RE.fullmatch(state["initial_status_sha256"]) is None
            or state.get("last_status_state") not in JOB_STATUS_RANK
            or not isinstance(state.get("last_status_sha256"), str)
            or SHA256_RE.fullmatch(state["last_status_sha256"]) is None
        ):
            raise ResearchInboxControlError("committed dispatch binding is incomplete")
    return state


def _write_state(
    paths: Mapping[str, Path],
    job_id: str,
    bundle_sha256: str,
    state: str,
    *,
    immutable_sha256s: Mapping[str, str] | None = None,
    initial_status_sha256: str | None = None,
    last_status_state: str | None = None,
    last_status_sha256: str | None = None,
    incoming_prepared: bool = False,
) -> None:
    if state not in REMOTE_LIFECYCLE_STATES:
        raise ResearchInboxControlError("invalid remote lifecycle state")
    value: dict[str, Any] = {
        "schema_version": SCHEMA_STATE,
        "job_id": job_id,
        "bundle_sha256": bundle_sha256,
        "state": state,
        "data_files_received": 0,
        "data_access": DATA_PLANE,
        "incoming_prepared": bool(incoming_prepared),
    }
    if immutable_sha256s is not None:
        value["immutable_sha256s"] = dict(immutable_sha256s)
    if initial_status_sha256 is not None:
        value["initial_status_sha256"] = initial_status_sha256
    if last_status_state is not None:
        value["last_status_state"] = last_status_state
    if last_status_sha256 is not None:
        value["last_status_sha256"] = last_status_sha256
    _atomic_json(_state_path(paths, job_id), value)


def _binding_from_controls(controls: Mapping[str, bytes]) -> dict[str, Any]:
    initial_status_sha = _sha(controls["STATUS.json"])
    return {
        "immutable_sha256s": {
            name: _sha(controls[name])
            for name in ("PLAN.md", "JOB_SPEC.json", "REQUEST.json")
        },
        "initial_status_sha256": initial_status_sha,
        "last_status_state": "QUEUED",
        "last_status_sha256": initial_status_sha,
    }


def _state_binding(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "immutable_sha256s": state.get("immutable_sha256s"),
        "initial_status_sha256": state.get("initial_status_sha256"),
        "last_status_state": state.get("last_status_state"),
        "last_status_sha256": state.get("last_status_sha256"),
    }


def _clean_incoming(path: Path) -> None:
    if path.is_symlink():
        raise ResearchInboxControlError("incoming job path is a symlink")
    if not path.exists():
        return
    if not path.is_dir():
        raise ResearchInboxControlError("incoming job path is not a directory")
    for child in path.iterdir():
        if child.name not in CONTROL_FILES or child.is_symlink() or not child.is_file():
            raise ResearchInboxControlError("incoming job contains an unexpected entry")
        child.unlink()
    path.rmdir()


def _validate_initial_controls(job_dir: Path, job_id: str) -> tuple[dict[str, bytes], str]:
    if job_dir.is_symlink() or not job_dir.is_dir():
        raise ResearchInboxControlError("job directory is missing or unsafe")
    entries = list(job_dir.iterdir())
    if {path.name for path in entries} != set(CONTROL_FILES):
        raise ResearchInboxControlError("job directory must contain exactly four control files")
    controls = {
        name: _read_regular(
            job_dir / name, label=name, limit=CONTROL_LIMITS[name]
        )
        for name in CONTROL_FILES
    }
    if any(_credential_like(payload) for payload in controls.values()):
        raise ResearchInboxControlError("control bundle contains credential-like content")
    try:
        controls["PLAN.md"].decode("utf-8")
    except UnicodeError as exc:
        raise ResearchInboxControlError("PLAN.md is not UTF-8") from exc
    spec = _json(controls["JOB_SPEC.json"], "JOB_SPEC.json")
    request = _json(controls["REQUEST.json"], "REQUEST.json")
    status_value = _json(controls["STATUS.json"], "STATUS.json")
    for name, value in (
        ("JOB_SPEC.json", spec),
        ("REQUEST.json", request),
        ("STATUS.json", status_value),
    ):
        if controls[name] != _canonical(value):
            raise ResearchInboxControlError("%s is not canonical JSON" % name)
    plan_sha = _sha(controls["PLAN.md"])
    if (
        spec.get("schema_version") != "research-job-spec-v1"
        or spec.get("state") != "READY"
        or not isinstance(spec.get("plugin_id"), str)
        or spec.get("plan_sha256") != plan_sha
        or spec.get("plan_bytes") != len(controls["PLAN.md"])
        or spec.get("execution_class") != "READONLY_EXPLORATORY"
        or spec.get("arbitrary_plan_code_execution") is not False
    ):
        raise ResearchInboxControlError("JOB_SPEC.json violates the ready read-only contract")
    requirements = spec.get("data_requirements")
    if (
        not isinstance(requirements, dict)
        or requirements.get("zero_copy") is not True
        or requirements.get("exact_version_required") is not True
    ):
        raise ResearchInboxControlError("JOB_SPEC.json is not exact-version zero-copy")
    safety = spec.get("safety")
    if not isinstance(safety, dict) or any(
        safety.get(field) is not False
        for field in (
            "s3_write",
            "production_mutation",
            "trading_credentials",
            "orders",
            "external_paid_api",
        )
    ) or safety.get("w09_isolated_execution") is not True:
        raise ResearchInboxControlError("JOB_SPEC.json safety boundary is incomplete")
    expected_request = {
        "schema_version": "research-job-request-v1",
        "job_id": job_id,
        "plan_sha256": plan_sha,
        "plan_bytes": len(controls["PLAN.md"]),
        "automatic_execution_requested": True,
        "execution_target": "W09_ISOLATED_RESEARCH",
        "data_access": "ZERO_COPY_EXACT_VERSION_READONLY",
        "arbitrary_plan_code_execution": False,
    }
    if any(request.get(field) != value for field, value in expected_request.items()):
        raise ResearchInboxControlError("REQUEST.json differs from the fixed W09 contract")
    if (
        status_value.get("schema_version") != "research-job-status-v1"
        or status_value.get("job_id") != job_id
        or status_value.get("state") != "QUEUED"
        or status_value.get("research_execution_started") is not False
    ):
        raise ResearchInboxControlError("STATUS.json is not an unstarted queued job")
    manifest = {
        "schema_version": SCHEMA_BUNDLE,
        "job_id": job_id,
        "files": [
            {"name": name, "bytes": len(controls[name]), "sha256": _sha(controls[name])}
            for name in CONTROL_FILES
        ],
        "data_files_transferred": 0,
        "data_access": DATA_PLANE,
    }
    return controls, _sha(_canonical(manifest))


def _validate_runtime_status(
    job_dir: Path, job_id: str, state: Mapping[str, Any]
) -> tuple[bytes, dict[str, Any]]:
    raw = _read_regular(
        job_dir / "STATUS.json",
        label="STATUS.json",
        limit=CONTROL_LIMITS["STATUS.json"],
    )
    value = _json(raw, "STATUS.json")
    current = value.get("state")
    previous = state.get("last_status_state")
    if (
        value.get("schema_version") != "research-job-status-v1"
        or value.get("job_id") != job_id
        or current not in JOB_STATUS_RANK
        or previous not in JOB_STATUS_RANK
        or raw != _canonical(value)
    ):
        raise ResearchInboxControlError("mutable STATUS.json violates its job contract")
    if JOB_STATUS_RANK[current] < JOB_STATUS_RANK[previous]:
        raise ResearchInboxControlError("mutable STATUS.json regressed")
    if previous in TERMINAL_JOB_STATES and current != previous:
        raise ResearchInboxControlError("mutable STATUS.json replaced a terminal state")
    if state.get("state") == "COMMITTED" and (
        current != "QUEUED"
        or _sha(raw) != state.get("initial_status_sha256")
        or value.get("research_execution_started") is not False
    ):
        raise ResearchInboxControlError("unstarted committed job STATUS.json drifted")
    return raw, value


def _validate_immutable_inputs(
    job_dir: Path, job_id: str, state: Mapping[str, Any]
) -> None:
    if job_dir.is_symlink() or not job_dir.is_dir():
        raise ResearchInboxControlError("committed job directory is unsafe")
    immutable = state.get("immutable_sha256s")
    for name in ("PLAN.md", "JOB_SPEC.json", "REQUEST.json"):
        raw = _read_regular(job_dir / name, label=name, limit=CONTROL_LIMITS[name])
        if _sha(raw) != immutable.get(name):
            raise ResearchInboxControlError(
                "committed immutable control drifted: %s" % name
            )


def prepare_receive(root: Path, job_id: str, bundle_sha256: str) -> dict[str, Any]:
    job_id = _job_id(job_id)
    bundle_sha256 = _bundle_sha(bundle_sha256)
    paths = _layout(root)
    with _lock(paths, job_id):
        state = _read_state(paths, job_id)
        if state is not None and state.get("state") in STARTED_LIFECYCLE_STATES:
            raise ResearchInboxControlError(
                "started or planned job cannot be prepared for overwrite"
            )
        if state is not None and state.get("bundle_sha256") not in {None, bundle_sha256}:
            raise ResearchInboxControlError("job id is already bound to another bundle")
        final = paths["jobs"] / job_id
        binding: dict[str, Any] | None = None
        if final.exists() or final.is_symlink():
            controls, existing_sha = _validate_initial_controls(final, job_id)
            if existing_sha != bundle_sha256:
                raise ResearchInboxControlError("committed job differs from requested bundle")
            binding = _binding_from_controls(controls)
        incoming = paths["incoming"] / job_id
        _clean_incoming(incoming)
        incoming.mkdir(mode=0o700)
        if binding is None:
            if state is not None and state.get("state") == "COMMITTED":
                raise ResearchInboxControlError("committed job directory disappeared")
            _write_state(
                paths,
                job_id,
                bundle_sha256,
                "PREPARED",
                incoming_prepared=True,
            )
        else:
            _write_state(
                paths,
                job_id,
                bundle_sha256,
                "COMMITTED",
                incoming_prepared=True,
                **binding,
            )
    return {"state": "PREPARED", "job_id": job_id, "bundle_sha256": bundle_sha256}


def abort_receive(root: Path, job_id: str, bundle_sha256: str) -> dict[str, Any]:
    job_id = _job_id(job_id)
    bundle_sha256 = _bundle_sha(bundle_sha256)
    paths = _layout(root)
    with _lock(paths, job_id):
        state = _read_state(paths, job_id)
        if (
            state is None
            or state.get("state") not in {"PREPARED", "COMMITTED"}
            or state.get("bundle_sha256") != bundle_sha256
            or state.get("incoming_prepared") is not True
        ):
            raise ResearchInboxControlError("abort does not bind the prepared bundle")
        _clean_incoming(paths["incoming"] / job_id)
        final = paths["jobs"] / job_id
        if final.exists() or final.is_symlink():
            controls, actual = _validate_initial_controls(final, job_id)
            if actual != bundle_sha256:
                raise ResearchInboxControlError("committed job drifted during abort")
            next_state = "COMMITTED"
            _write_state(
                paths,
                job_id,
                bundle_sha256,
                next_state,
                **_binding_from_controls(controls),
            )
        else:
            next_state = "ABORTED"
            _write_state(paths, job_id, bundle_sha256, next_state)
    return {"state": next_state, "job_id": job_id, "bundle_sha256": bundle_sha256}


def commit_receive(root: Path, job_id: str, bundle_sha256: str) -> dict[str, Any]:
    job_id = _job_id(job_id)
    bundle_sha256 = _bundle_sha(bundle_sha256)
    paths = _layout(root)
    with _lock(paths, job_id):
        state = _read_state(paths, job_id)
        if (
            state is None
            or state.get("state") not in {"PREPARED", "COMMITTED"}
            or state.get("bundle_sha256") != bundle_sha256
            or state.get("incoming_prepared") is not True
        ):
            raise ResearchInboxControlError("bundle was not prepared by this protocol")
        incoming = paths["incoming"] / job_id
        controls, actual_sha = _validate_initial_controls(incoming, job_id)
        if actual_sha != bundle_sha256:
            raise ResearchInboxControlError("uploaded controls differ from the prepared bundle")
        final = paths["jobs"] / job_id
        idempotent = False
        if final.exists() or final.is_symlink():
            _, existing_sha = _validate_initial_controls(final, job_id)
            if existing_sha != bundle_sha256:
                raise ResearchInboxControlError("job id collision with another committed bundle")
            _clean_incoming(incoming)
            idempotent = True
        else:
            for name in CONTROL_FILES:
                (incoming / name).chmod(0o440)
            incoming.chmod(0o750)
            os.rename(incoming, final)
            directory = os.open(paths["jobs"], os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        _write_state(
            paths,
            job_id,
            bundle_sha256,
            "COMMITTED",
            **_binding_from_controls(controls),
        )
    return {
        "state": "COMMITTED_NOT_STARTED",
        "job_id": job_id,
        "bundle_sha256": bundle_sha256,
        "idempotent": idempotent,
        "data_files_received": 0,
        "data_access": DATA_PLANE,
    }


def _committed_snapshot(
    root: Path, job_id: str, bundle_sha256: str | None = None
) -> tuple[str, dict[str, Any], bytes, dict[str, Any]]:
    job_id = _job_id(job_id)
    paths = _layout(root)
    state = _read_state(paths, job_id)
    if state is None or state.get("state") not in {
        "COMMITTED",
        *STARTED_LIFECYCLE_STATES,
    }:
        raise ResearchInboxControlError("job is not atomically committed")
    expected = _bundle_sha(state.get("bundle_sha256"))
    if bundle_sha256 is not None and _bundle_sha(bundle_sha256) != expected:
        raise ResearchInboxControlError("committed bundle SHA-256 mismatch")
    job_dir = paths["jobs"] / job_id
    _validate_immutable_inputs(job_dir, job_id, state)
    status_raw, status_value = _validate_runtime_status(job_dir, job_id, state)
    return expected, state, status_raw, status_value


def _arm_path(run_root: Path, job_id: str) -> Path:
    return Path(run_root) / (job_id + ".json")


def verify_start_arm(root: Path, run_root: Path, job_id: str) -> dict[str, Any]:
    job_id = _job_id(job_id)
    arm_path = _arm_path(run_root, job_id)
    arm = _json(_read_regular(arm_path, label="explicit start arm", limit=64 * 1024), "start arm")
    if (
        arm.get("schema_version") != SCHEMA_ARM
        or arm.get("job_id") != job_id
        or arm.get("worker_unit") != WORKER_UNIT_TEMPLATE
        or arm.get("expires_at_epoch") is None
        or not isinstance(arm.get("expires_at_epoch"), (int, float))
        or time.time() > float(arm["expires_at_epoch"])
    ):
        raise ResearchInboxControlError("explicit start arm is invalid or expired")
    bundle, state, status_raw, status_value = _committed_snapshot(
        root, job_id, arm.get("bundle_sha256")
    )
    if (
        state.get("state") != "START_ARMED"
        or status_value.get("state") != "QUEUED"
        or _sha(status_raw) != state.get("initial_status_sha256")
    ):
        raise ResearchInboxControlError("explicit start arm does not bind a pristine queued job")
    return {"state": "EXPLICIT_START_ARM_VALID", "job_id": job_id, "bundle_sha256": bundle}


def start_committed_job(
    root: Path,
    run_root: Path,
    job_id: str,
    bundle_sha256: str,
    *,
    runner: Runner = subprocess.run,
    require_root: bool = True,
) -> dict[str, Any]:
    job_id = _job_id(job_id)
    bundle_sha256 = _bundle_sha(bundle_sha256)
    if require_root and os.geteuid() != 0:
        raise ResearchInboxControlError("explicit start must use the installed sudo boundary")
    paths = _layout(root)
    with _lock(paths, job_id):
        _expected, state, status_raw, status_value = _committed_snapshot(
            root, job_id, bundle_sha256
        )
        if (
            state.get("state") != "COMMITTED"
            or state.get("incoming_prepared") is not False
            or status_value.get("state") != "QUEUED"
            or _sha(status_raw) != state.get("initial_status_sha256")
        ):
            raise ResearchInboxControlError(
                "explicit start requires the pristine initial committed job"
            )
        _controls, initial_bundle = _validate_initial_controls(
            paths["jobs"] / job_id, job_id
        )
        if initial_bundle != bundle_sha256:
            raise ResearchInboxControlError("initial committed bundle drifted before start")
        run_root = Path(run_root)
        if run_root.is_symlink():
            raise ResearchInboxControlError("start arm root is a symlink")
        run_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        arm_path = _arm_path(run_root, job_id)
        if arm_path.exists() or arm_path.is_symlink():
            raise ResearchInboxControlError("explicit start arm already exists")
        try:
            _atomic_json(
                arm_path,
                {
                    "schema_version": SCHEMA_ARM,
                    "job_id": job_id,
                    "bundle_sha256": bundle_sha256,
                    "worker_unit": WORKER_UNIT_TEMPLATE,
                    "controller_pid": os.getpid(),
                    "expires_at_epoch": time.time() + ARM_MAX_AGE_SECONDS,
                },
            )
            arm_path.chmod(0o400)
            _write_state(
                paths,
                job_id,
                bundle_sha256,
                "START_ARMED",
                **_state_binding(state),
            )
        except Exception:
            try:
                arm_path.unlink()
            except FileNotFoundError:
                pass
            raise
        unit = WORKER_UNIT_TEMPLATE.replace("@.", "@%s." % job_id)
        failure: ResearchInboxControlError | None = None
        try:
            result = runner(
                [SYSTEMCTL, "start", unit],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                shell=False,
                timeout=1800,
            )
            if result.returncode != 0:
                failure = ResearchInboxControlError(
                    "fixed generic worker returned rc=%s" % result.returncode
                )
        except (OSError, subprocess.SubprocessError) as exc:
            failure = ResearchInboxControlError("fixed generic worker could not start")
            failure.__cause__ = exc
        finally:
            try:
                arm_path.unlink()
            except FileNotFoundError:
                pass
        try:
            _current_bundle, armed_state, current_raw, current_status = _committed_snapshot(
                root, job_id, bundle_sha256
            )
            if failure is None and current_status.get("state") == "QUEUED":
                failure = ResearchInboxControlError(
                    "generic planning worker returned without advancing STATUS.json"
                )
            _write_state(
                paths,
                job_id,
                bundle_sha256,
                "WORKER_INVOKED" if failure is None else "START_FAILED",
                immutable_sha256s=armed_state["immutable_sha256s"],
                initial_status_sha256=armed_state["initial_status_sha256"],
                last_status_state=current_status["state"],
                last_status_sha256=_sha(current_raw),
            )
        except ResearchInboxControlError:
            _write_state(
                paths,
                job_id,
                bundle_sha256,
                "START_FAILED",
                **_state_binding(state),
            )
            raise
        if failure is not None:
            raise failure
    return {
        "state": "GENERIC_PLANNING_WORKER_COMPLETED",
        "job_id": job_id,
        "bundle_sha256": bundle_sha256,
        "worker_unit": unit,
        "research_execution_started": False,
        "planning_status": current_status["state"],
    }


def _safe_report_file(report_root: Path, path: Path) -> PurePosixPath:
    if path.is_symlink() or not path.is_file():
        raise ResearchInboxControlError("REPORT contains a link or special file")
    relative = path.relative_to(report_root)
    if (
        not relative.parts
        or len(relative.parts) + 1 > MAX_REPORT_DEPTH
        or any(REPORT_COMPONENT_RE.fullmatch(part) is None for part in relative.parts)
    ):
        raise ResearchInboxControlError("REPORT contains an unsafe path")
    return PurePosixPath("REPORT", *relative.parts)


def _expected_output_receipt(
    job_id: str, artifacts: Mapping[PurePosixPath, bytes]
) -> dict[str, Any]:
    rows = [
        {"path": str(path), "bytes": len(payload), "sha256": _sha(payload)}
        for path, payload in sorted(artifacts.items(), key=lambda item: str(item[0]))
    ]
    value: dict[str, Any] = {
        "schema_version": OUTPUT_SCHEMA,
        "job_id": job_id,
        "output_directory": "OUTPUT",
        "artifact_count": len(rows),
        "artifact_bytes": sum(row["bytes"] for row in rows),
        "artifacts": rows,
        "data_files_exported": 0,
        "source_contract": "DEDICATED_JOB_OUTPUT_ONLY",
    }
    value["output_sha256"] = _sha(_canonical(value))
    return value


def _dedicated_output_snapshot(
    job_dir: Path, job_id: str
) -> dict[PurePosixPath, bytes]:
    """Validate and snapshot only ``<job>/OUTPUT`` result artifacts."""
    output = job_dir / "OUTPUT"
    if output.is_symlink() or not output.is_dir():
        raise ResearchInboxControlError("completed job OUTPUT is missing or unsafe")
    entries = list(output.iterdir())
    if {path.name for path in entries} != {
        "RESULTS.json",
        "REPORT",
        "OUTPUT_RECEIPT.json",
    }:
        raise ResearchInboxControlError("completed job OUTPUT has unexpected entries")
    receipt_first = _read_regular(
        output / "OUTPUT_RECEIPT.json",
        label="OUTPUT/OUTPUT_RECEIPT.json",
        limit=CONTROL_LIMITS["STATUS.json"],
    )
    results = _read_regular(
        output / "RESULTS.json",
        label="OUTPUT/RESULTS.json",
        limit=MAX_EXPORT_FILE_BYTES,
    )
    report = output / "REPORT"
    if report.is_symlink() or not report.is_dir():
        raise ResearchInboxControlError("completed job OUTPUT/REPORT is missing or unsafe")
    artifacts: dict[PurePosixPath, bytes] = {
        PurePosixPath("RESULTS.json"): results,
    }
    for path in sorted(report.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        relative = _safe_report_file(report, path)
        artifacts[relative] = _read_regular(
            path, label="OUTPUT/%s" % relative, limit=MAX_EXPORT_FILE_BYTES
        )
    index_path = PurePosixPath("REPORT/index.html")
    report_receipt_path = PurePosixPath("REPORT/REPORT_RECEIPT.json")
    if index_path not in artifacts or report_receipt_path not in artifacts:
        raise ResearchInboxControlError("completed job lacks its report contract")
    # STATUS.json and OUTPUT_RECEIPT.json consume the other two export slots.
    if len(artifacts) + 2 > MAX_EXPORT_FILES:
        raise ResearchInboxControlError("output snapshot contains too many artifacts")
    if sum(len(value) for value in artifacts.values()) > MAX_EXPORT_BYTES:
        raise ResearchInboxControlError("output snapshot exceeds 64 MiB")
    for path, payload in artifacts.items():
        if path.suffix.lower() in {".json", ".html", ".css", ".csv", ".txt", ".md", ".svg"}:
            if _credential_like(payload):
                raise ResearchInboxControlError(
                    "output snapshot contains credential-like content"
                )

    results_value = _json(results, "OUTPUT/RESULTS.json")
    if (
        results_value.get("schema_version") != RESULTS_SCHEMA
        or results_value.get("job_id") != job_id
    ):
        raise ResearchInboxControlError("OUTPUT/RESULTS.json does not bind this job")
    report_receipt = _json(
        artifacts[report_receipt_path], "OUTPUT/REPORT/REPORT_RECEIPT.json"
    )
    if (
        report_receipt.get("schema_version") != REPORT_RECEIPT_SCHEMA
        or report_receipt.get("job_id") != job_id
        or report_receipt.get("results_sha256") != _sha(results)
        or report_receipt.get("report_sha256") != _sha(artifacts[index_path])
    ):
        raise ResearchInboxControlError("report receipt does not bind results/report bytes")
    receipt = _json(receipt_first, "OUTPUT/OUTPUT_RECEIPT.json")
    expected = _expected_output_receipt(job_id, artifacts)
    if receipt != expected or receipt_first != _canonical(receipt):
        raise ResearchInboxControlError("OUTPUT receipt or artifact hashes differ")
    receipt_second = _read_regular(
        output / "OUTPUT_RECEIPT.json",
        label="OUTPUT/OUTPUT_RECEIPT.json",
        limit=CONTROL_LIMITS["STATUS.json"],
    )
    if _sha(receipt_first) != _sha(receipt_second):
        raise ResearchInboxControlError("OUTPUT receipt changed during snapshot")
    files = {PurePosixPath("OUTPUT_RECEIPT.json"): receipt_first}
    files.update(artifacts)
    return files


def _output_snapshot(
    job_dir: Path, job_id: str, state: Mapping[str, Any]
) -> dict[PurePosixPath, bytes]:
    first, status_value = _validate_runtime_status(job_dir, job_id, state)
    files: dict[PurePosixPath, bytes] = {PurePosixPath("STATUS.json"): first}
    if status_value.get("state") == "COMPLETE":
        files.update(_dedicated_output_snapshot(job_dir, job_id))
    second = _read_regular(job_dir / "STATUS.json", label="STATUS.json", limit=1024 * 1024)
    if _sha(first) != _sha(second):
        raise ResearchInboxControlError("STATUS.json changed during output snapshot")
    if len(files) > MAX_EXPORT_FILES:
        raise ResearchInboxControlError("output snapshot contains too many artifacts")
    total = sum(len(value) for value in files.values())
    if total > MAX_EXPORT_BYTES:
        raise ResearchInboxControlError("output snapshot exceeds 64 MiB")
    for path, payload in files.items():
        if path.suffix.lower() in {".json", ".html", ".css", ".csv", ".txt", ".md", ".svg"}:
            if _credential_like(payload):
                raise ResearchInboxControlError("output snapshot contains credential-like content")
    return files


def export_outputs(root: Path, job_id: str) -> bytes:
    job_id = _job_id(job_id)
    paths = _layout(root)
    with _lock(paths, job_id):
        _bundle, state, _status_raw, _status_value = _committed_snapshot(root, job_id)
        files = _output_snapshot(paths["jobs"] / job_id, job_id, state)
        exported_status_raw = files[PurePosixPath("STATUS.json")]
        exported_status = _json(exported_status_raw, "STATUS.json")
        _write_state(
            paths,
            job_id,
            state["bundle_sha256"],
            state["state"],
            immutable_sha256s=state["immutable_sha256s"],
            initial_status_sha256=state["initial_status_sha256"],
            last_status_state=exported_status["state"],
            last_status_sha256=_sha(exported_status_raw),
            incoming_prepared=state["incoming_prepared"],
        )
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path, payload in sorted(files.items(), key=lambda item: str(item[0])):
            info = tarfile.TarInfo(str(path))
            info.size = len(payload)
            info.mode = 0o440
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            archive.addfile(info, io.BytesIO(payload))
    raw = output.getvalue()
    if len(raw) > MAX_EXPORT_BYTES:
        raise ResearchInboxControlError("encoded output snapshot exceeds 64 MiB")
    return raw


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in (
        "receive-prepare",
        "receive-commit",
        "receive-abort",
        "start",
        "export",
        "verify-start",
    ):
        command = subparsers.add_parser(action)
        command.add_argument("--protocol", required=True, choices=[PROTOCOL])
        command.add_argument("--job-id", required=True)
        if action in {"receive-prepare", "receive-commit", "receive-abort", "start"}:
            command.add_argument("--bundle-sha256", required=True)
        if action == "export":
            command.add_argument("--format", required=True, choices=[EXPORT_FORMAT])
            command.add_argument("--artifact-set", required=True, choices=[ARTIFACT_SET])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # Only start needs privilege.  Re-entry is limited by the installed sudoers
    # rule and this parser; all other SSH-facing actions stay unprivileged.
    if args.action == "start" and os.geteuid() != 0:
        os.execv(
            "/usr/bin/sudo",
            ["sudo", "-n", str(CONTROL_WRAPPER), *(list(argv) if argv is not None else sys.argv[1:])],
        )
    try:
        if args.action == "receive-prepare":
            result = prepare_receive(INBOX_ROOT, args.job_id, args.bundle_sha256)
        elif args.action == "receive-commit":
            result = commit_receive(INBOX_ROOT, args.job_id, args.bundle_sha256)
        elif args.action == "receive-abort":
            result = abort_receive(INBOX_ROOT, args.job_id, args.bundle_sha256)
        elif args.action == "start":
            if os.environ.get("SUDO_USER") != "ubuntu":
                raise ResearchInboxControlError("explicit start caller must be sudo user ubuntu")
            result = start_committed_job(
                INBOX_ROOT, RUN_ARM_ROOT, args.job_id, args.bundle_sha256
            )
        elif args.action == "verify-start":
            result = verify_start_arm(INBOX_ROOT, RUN_ARM_ROOT, args.job_id)
        else:
            sys.stdout.buffer.write(export_outputs(INBOX_ROOT, args.job_id))
            sys.stdout.buffer.flush()
            return 0
    except ResearchInboxControlError as exc:
        print("W09_RESEARCH_INBOX_CONTROL_REFUSED: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
