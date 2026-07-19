#!/usr/bin/env python3
"""Immutable local spool for general Research Inbox jobs.

The inbox stores plans and canonical job specs.  It never executes plan text,
opens network connections, or interprets Markdown as Python/SQL.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import threading
from typing import Any


SCHEMA_REQUEST = "research-job-request-v1"
SCHEMA_STATUS = "research-job-status-v1"
MAX_PLAN_BYTES = 2 * 1024 * 1024
JOB_ID_RE = re.compile(r"^RJOB-\d{8}T\d{12}Z-[0-9a-f]{12}$")
REPORT_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SCHEMA_OUTPUT_RECEIPT = "research-job-output-receipt-v1"
LOCAL_OUTPUT_RECEIPT = "W09_OUTPUT_RECEIPT.json"
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_FILE_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_FILES = 256
MAX_REPORT_DEPTH = 8
_OUTPUT_VALIDATION_CACHE: dict[str, tuple[Any, ...]] = {}
_OUTPUT_CACHE_LOCK = threading.Lock()
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


def _stable_bytes(path: Path, label: str, limit: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise InboxError("%s is missing or unsafe" % label) from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > limit
        ):
            raise InboxError("%s is not a bounded regular file" % label)
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    fingerprint_before = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    fingerprint_after = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if fingerprint_before != fingerprint_after or len(raw) != before.st_size:
        raise InboxError("%s changed while being read" % label)
    return raw


def _json_bytes(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise InboxError("%s is invalid JSON" % label) from exc
    if not isinstance(value, dict):
        raise InboxError("%s root is not an object" % label)
    return value


def _safe_output_path(value: Any) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise InboxError("output receipt contains an unsafe path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise InboxError("output receipt contains path traversal")
    if any(REPORT_COMPONENT_RE.fullmatch(part) is None for part in path.parts):
        raise InboxError("output receipt contains an invalid path component")
    if len(path.parts) > MAX_REPORT_DEPTH:
        raise InboxError("output receipt path is too deep")
    if path == PurePosixPath("RESULTS.json"):
        return path
    if len(path.parts) < 2 or path.parts[0] != "REPORT":
        raise InboxError("output receipt contains an unapproved artifact")
    return path


def _expected_output_receipt(
    job_id: str, artifacts: dict[PurePosixPath, bytes]
) -> dict[str, Any]:
    rows = [
        {
            "path": str(path),
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for path, payload in sorted(artifacts.items(), key=lambda item: str(item[0]))
    ]
    value: dict[str, Any] = {
        "schema_version": SCHEMA_OUTPUT_RECEIPT,
        "job_id": job_id,
        "output_directory": "OUTPUT",
        "artifact_count": len(rows),
        "artifact_bytes": sum(row["bytes"] for row in rows),
        "artifacts": rows,
        "data_files_exported": 0,
        "source_contract": "DEDICATED_JOB_OUTPUT_ONLY",
    }
    value["output_sha256"] = hashlib.sha256(_canonical_json_bytes(value)).hexdigest()
    return value


def _validated_output_tree(
    artifact_root: Path, receipt_path: Path, job_id: str
) -> dict[PurePosixPath, bytes] | None:
    try:
        receipt_raw = _stable_bytes(
            receipt_path,
            receipt_path.name,
            1024 * 1024,
        )
        receipt = _json_bytes(receipt_raw, LOCAL_OUTPUT_RECEIPT)
        if receipt_raw != _canonical_json_bytes(receipt):
            raise InboxError("local output receipt is not canonical JSON")
        rows = receipt.get("artifacts")
        if not isinstance(rows, list) or not rows or len(rows) + 2 > MAX_OUTPUT_FILES:
            raise InboxError("local output receipt artifact list is invalid")
        artifacts: dict[PurePosixPath, bytes] = {}
        folded: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"path", "bytes", "sha256"}:
                raise InboxError("local output receipt artifact entry is invalid")
            relative = _safe_output_path(row["path"])
            key = str(relative).casefold()
            if key in folded:
                raise InboxError("local output receipt contains duplicate artifacts")
            folded.add(key)
            expected_bytes = row.get("bytes")
            expected_sha = row.get("sha256")
            if (
                not isinstance(expected_bytes, int)
                or isinstance(expected_bytes, bool)
                or expected_bytes <= 0
                or expected_bytes > MAX_OUTPUT_FILE_BYTES
                or not isinstance(expected_sha, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None
            ):
                raise InboxError("local output receipt artifact bounds are invalid")
            raw = _stable_bytes(
                artifact_root.joinpath(*relative.parts),
                "local output artifact",
                MAX_OUTPUT_FILE_BYTES,
            )
            if len(raw) != expected_bytes or hashlib.sha256(raw).hexdigest() != expected_sha:
                raise InboxError("local output artifact differs from its receipt")
            artifacts[relative] = raw
        if sum(len(raw) for raw in artifacts.values()) > MAX_OUTPUT_BYTES:
            raise InboxError("local output artifacts exceed 64 MiB")

        report_root = artifact_root / "REPORT"
        if report_root.is_symlink() or not report_root.is_dir():
            raise InboxError("local REPORT is missing or unsafe")
        actual_report_paths: set[PurePosixPath] = set()
        for path in report_root.rglob("*"):
            if path.is_symlink():
                raise InboxError("local REPORT contains a symlink")
            if path.is_dir():
                continue
            if not path.is_file():
                raise InboxError("local REPORT contains a special file")
            relative = PurePosixPath("REPORT", *path.relative_to(report_root).parts)
            if _safe_output_path(str(relative)) != relative:
                raise InboxError("local REPORT contains an unsafe path")
            actual_report_paths.add(relative)
        receipt_report_paths = {path for path in artifacts if path.parts[0] == "REPORT"}
        if actual_report_paths != receipt_report_paths:
            raise InboxError("local REPORT tree differs from its output receipt")

        results_path = PurePosixPath("RESULTS.json")
        index_path = PurePosixPath("REPORT/index.html")
        report_receipt_path = PurePosixPath("REPORT/REPORT_RECEIPT.json")
        if not {results_path, index_path, report_receipt_path}.issubset(artifacts):
            raise InboxError("local output lacks its report contract")
        results = _json_bytes(artifacts[results_path], "RESULTS.json")
        report_receipt = _json_bytes(
            artifacts[report_receipt_path], "REPORT_RECEIPT.json"
        )
        if (
            results.get("schema_version") != "research-results-v1"
            or results.get("job_id") != job_id
            or report_receipt.get("schema_version") != "research-report-receipt-v1"
            or report_receipt.get("job_id") != job_id
            or report_receipt.get("results_sha256")
            != hashlib.sha256(artifacts[results_path]).hexdigest()
            or report_receipt.get("report_sha256")
            != hashlib.sha256(artifacts[index_path]).hexdigest()
        ):
            raise InboxError("local result/report binding is invalid")
        if receipt != _expected_output_receipt(job_id, artifacts):
            raise InboxError("local output receipt or tree hash differs")
        return artifacts
    except (InboxError, OSError):
        return None


def _output_metadata_signature(
    artifact_root: Path, receipt_path: Path
) -> tuple[Any, ...] | None:
    """Return a cheap mutation-sensitive signature without rehashing artifacts."""
    try:
        receipt_raw = _stable_bytes(receipt_path, receipt_path.name, 1024 * 1024)
        receipt = _json_bytes(receipt_raw, receipt_path.name)
        rows = receipt.get("artifacts")
        if not isinstance(rows, list) or not rows or len(rows) + 2 > MAX_OUTPUT_FILES:
            raise InboxError("output receipt artifact list is invalid")
        values: list[tuple[Any, ...]] = [
            ("receipt", hashlib.sha256(receipt_raw).hexdigest())
        ]
        expected_report: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise InboxError("output receipt row is invalid")
            relative = _safe_output_path(row.get("path"))
            path = artifact_root.joinpath(*relative.parts)
            metadata = os.stat(path, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode):
                raise InboxError("output artifact is not regular")
            values.append(
                (
                    str(relative),
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                )
            )
            if relative.parts[0] == "REPORT":
                expected_report.add(str(relative))
        report_root = artifact_root / "REPORT"
        if report_root.is_symlink() or not report_root.is_dir():
            raise InboxError("REPORT is unsafe")
        actual_report: set[str] = set()
        for path in report_root.rglob("*"):
            if path.is_symlink():
                raise InboxError("REPORT contains a symlink")
            if path.is_dir():
                continue
            if not path.is_file():
                raise InboxError("REPORT contains a special file")
            relative = PurePosixPath("REPORT", *path.relative_to(report_root).parts)
            actual_report.add(str(_safe_output_path(str(relative))))
        if actual_report != expected_report:
            raise InboxError("REPORT tree differs from receipt")
        return tuple(sorted(values, key=lambda value: str(value[0])))
    except (InboxError, OSError, TypeError, ValueError):
        return None


def _validated_local_output(
    job_dir: Path,
    job_id: str,
    status_value: dict[str, Any],
    *,
    use_cache: bool = True,
) -> dict[PurePosixPath, bytes] | None:
    if (
        status_value.get("state") != "COMPLETE"
        or status_value.get("research_execution_started") is not True
        or status_value.get("report_ready") is not True
    ):
        return None
    receipt_path = job_dir / LOCAL_OUTPUT_RECEIPT
    signature = _output_metadata_signature(job_dir, receipt_path)
    cache_key = str(job_dir)
    if signature is None:
        with _OUTPUT_CACHE_LOCK:
            _OUTPUT_VALIDATION_CACHE.pop(cache_key, None)
        return None
    if use_cache:
        with _OUTPUT_CACHE_LOCK:
            if _OUTPUT_VALIDATION_CACHE.get(cache_key) == signature:
                # A non-None marker is sufficient for list/get availability.
                return {PurePosixPath("REPORT/index.html"): b""}
    artifacts = _validated_output_tree(job_dir, receipt_path, job_id)
    if artifacts is None:
        with _OUTPUT_CACHE_LOCK:
            _OUTPUT_VALIDATION_CACHE.pop(cache_key, None)
        return None
    with _OUTPUT_CACHE_LOCK:
        _OUTPUT_VALIDATION_CACHE[cache_key] = signature
    return artifacts


def _validated_remote_output(job_dir: Path, job_id: str) -> bool:
    output = job_dir / "OUTPUT"
    try:
        if output.is_symlink() or not output.is_dir():
            return False
        if {path.name for path in output.iterdir()} != {
            "RESULTS.json",
            "REPORT",
            "OUTPUT_RECEIPT.json",
        }:
            return False
    except OSError:
        return False
    return (
        _validated_output_tree(output, output / "OUTPUT_RECEIPT.json", job_id)
        is not None
    )


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
    coordination = None
    coordination_path = path / "W09_COORDINATOR_STATUS.json"
    if coordination_path.exists() or coordination_path.is_symlink():
        try:
            coordination = _read_json(coordination_path, "W09 coordinator status")
        except InboxError:
            coordination = {
                "state": "UNREADABLE",
                "message": "W09 coordinator status is unsafe or unreadable.",
            }
    validated_output = _validated_local_output(path, job_id, status)
    return {
        "job_id": job_id,
        "request": request,
        "status": status,
        "spec": spec,
        "coordination": coordination,
        "report_available": validated_output is not None,
    }


def read_report(inbox_root: Path, job_id: str) -> bytes:
    """Return only a receipt-validated report snapshot for browser serving."""
    root = _root(inbox_root)
    if not isinstance(job_id, str) or JOB_ID_RE.fullmatch(job_id) is None:
        raise InboxError("invalid job id")
    path = root / "jobs" / job_id
    if path.is_symlink() or not path.is_dir():
        raise InboxError("job not found")
    status = _read_json(path / "STATUS.json", "job status")
    artifacts = _validated_local_output(path, job_id, status, use_cache=False)
    if artifacts is None:
        raise InboxError("report not ready or output receipt is invalid")
    return artifacts[PurePosixPath("REPORT/index.html")]


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
    if state == "COMPLETE":
        job_dir = root / "jobs" / job_id
        if not _validated_remote_output(job_dir, job_id):
            raise InboxError(
                "COMPLETE requires a sealed, hash-validated dedicated OUTPUT bundle"
            )
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
