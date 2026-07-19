#!/usr/bin/env python3
"""Fail-closed Mac control-plane transport for the W09 Research Inbox.

Only four small, immutable control files cross from the Mac to W09.  Market
data never crosses this transport: the remote generic worker must resolve and
read exact S3 object versions with W09's instance profile.

The remote helper named by :data:`W09_INTERFACE` is a deliberately fixed
protocol boundary.  This client never sends plan text as a command, never
constructs a shell command, and never accepts a caller-provided host, remote
root, helper, or systemd unit.  ``dispatch`` commits a job but does not start
it; starting research is a separate explicit operation.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from typing import Any, Callable, Iterator, Mapping, Sequence


JOB_ID_RE = re.compile(r"^RJOB-\d{8}T\d{12}Z-[0-9a-f]{12}$")
SAFE_REMOTE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_./:=@-]+$")
SAFE_REPORT_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

CONTROL_FILES = ("PLAN.md", "JOB_SPEC.json", "REQUEST.json", "STATUS.json")
CONTROL_FILE_LIMITS = {
    "PLAN.md": 2 * 1024 * 1024,
    "JOB_SPEC.json": 1024 * 1024,
    "REQUEST.json": 1024 * 1024,
    "STATUS.json": 1024 * 1024,
}
MAX_EXPORT_BYTES = 64 * 1024 * 1024
MAX_EXPORT_FILE_BYTES = 32 * 1024 * 1024
MAX_EXPORT_FILES = 256
MAX_REPORT_DEPTH = 8

SCHEMA_BUNDLE = "research-w09-control-bundle-v1"
SCHEMA_JOB_SPEC = "research-job-spec-v1"
SCHEMA_REQUEST = "research-job-request-v1"
SCHEMA_STATUS = "research-job-status-v1"
SCHEMA_RESULTS = "research-results-v1"

STATUS_STATES = {
    "NEEDS_METHOD",
    "QUEUED",
    "PREFLIGHT",
    "READY",
    "RUNNING",
    "COMPLETE",
    "BLOCKED",
    "FAILED",
    "REFUSED",
}
STATUS_RANK = {
    "NEEDS_METHOD": 0,
    "QUEUED": 1,
    "PREFLIGHT": 2,
    "READY": 3,
    "RUNNING": 4,
    "COMPLETE": 5,
    "BLOCKED": 5,
    "FAILED": 5,
    "REFUSED": 5,
}
TERMINAL_STATES = {"COMPLETE", "BLOCKED", "FAILED", "REFUSED"}

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


@dataclass(frozen=True)
class W09RemoteInterface:
    """Pinned counterpart contract; none of these values are caller input."""

    schema_version: str
    ssh_target: str
    jobs_root: PurePosixPath
    incoming_root: PurePosixPath
    control_helper: PurePosixPath
    worker_unit_template: str
    export_format: str
    data_plane: str


W09_INTERFACE = W09RemoteInterface(
    schema_version="research-w09-dispatch-interface-v1",
    ssh_target="ubuntu@18.226.151.192",
    jobs_root=PurePosixPath("/srv/w09-research/inbox/jobs"),
    incoming_root=PurePosixPath("/srv/w09-research/inbox/.incoming"),
    control_helper=PurePosixPath("/usr/local/libexec/w09-research-inbox-control"),
    worker_unit_template="w09-research-inbox-worker@.service",
    export_format="research-w09-output-tar-v1",
    data_plane="W09_INSTANCE_PROFILE_EXACT_VERSION_READONLY",
)

DEFAULT_SSH_KEY = Path("~/.ssh/kalshi-key.pem").expanduser()


class W09DispatchError(ValueError):
    """The job or remote exchange failed a control-plane safety contract."""


@dataclass(frozen=True)
class StableFile:
    path: Path
    payload: bytes
    sha256: str
    fingerprint: tuple[int, int, int, int, int, int]


Runner = Callable[..., subprocess.CompletedProcess]


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _valid_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or JOB_ID_RE.fullmatch(job_id) is None:
        raise W09DispatchError("invalid research job id")
    return job_id


def _reject_symlink(path: Path, label: str) -> None:
    try:
        if path.is_symlink():
            raise W09DispatchError("%s cannot be a symlink" % label)
    except OSError as exc:
        raise W09DispatchError("%s cannot be inspected" % label) from exc


def _safe_job_dir(inbox_root: Path, job_id: str) -> tuple[Path, Path]:
    job_id = _valid_job_id(job_id)
    root = Path(inbox_root).expanduser()
    _reject_symlink(root, "inbox root")
    if not root.is_dir():
        raise W09DispatchError("inbox root is missing")
    jobs = root / "jobs"
    _reject_symlink(jobs, "inbox jobs root")
    if not jobs.is_dir():
        raise W09DispatchError("inbox jobs root is missing")
    job_dir = jobs / job_id
    _reject_symlink(job_dir, "job directory")
    if not job_dir.is_dir():
        raise W09DispatchError("research job is missing")
    return jobs, job_dir


def _fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_stable_file(path: Path, label: str, limit: int) -> StableFile:
    """Read one regular file without following a link or accepting mutation."""
    path = Path(path)
    _reject_symlink(path, label)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise W09DispatchError("%s is missing or unsafe" % label) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise W09DispatchError("%s is not a regular file" % label)
        if before.st_size < 0 or before.st_size > limit:
            raise W09DispatchError("%s exceeds its size limit" % label)
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
    if _fingerprint(before) != _fingerprint(after) or len(payload) != before.st_size:
        raise W09DispatchError("%s drifted while it was read" % label)
    return StableFile(
        path=path,
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        fingerprint=_fingerprint(after),
    )


def _has_credential(payload: bytes) -> bool:
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


def _json_object(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise W09DispatchError("%s is not valid UTF-8 JSON" % label) from exc
    if not isinstance(value, dict):
        raise W09DispatchError("%s JSON root must be an object" % label)
    return value


def _read_control_files(job_dir: Path, job_id: str) -> dict[str, StableFile]:
    files = {
        name: _read_stable_file(
            job_dir / name, "job %s" % name, CONTROL_FILE_LIMITS[name]
        )
        for name in CONTROL_FILES
    }
    for name, item in files.items():
        if _has_credential(item.payload):
            raise W09DispatchError("job %s contains credential-like content" % name)

    try:
        files["PLAN.md"].payload.decode("utf-8")
    except UnicodeError as exc:
        raise W09DispatchError("PLAN.md is not valid UTF-8") from exc
    spec = _json_object(files["JOB_SPEC.json"].payload, "JOB_SPEC.json")
    request = _json_object(files["REQUEST.json"].payload, "REQUEST.json")
    status_value = _json_object(files["STATUS.json"].payload, "STATUS.json")

    if spec.get("schema_version") != SCHEMA_JOB_SPEC or spec.get("state") != "READY":
        raise W09DispatchError("job spec is not a READY registered-method job")
    if not isinstance(spec.get("plugin_id"), str) or not spec["plugin_id"]:
        raise W09DispatchError("job spec has no registered method plugin")
    if spec.get("execution_class") != "READONLY_EXPLORATORY":
        raise W09DispatchError("job spec is not read-only exploratory")
    if spec.get("arbitrary_plan_code_execution") is not False:
        raise W09DispatchError("job spec does not forbid arbitrary plan code")
    requirements = spec.get("data_requirements")
    if (
        not isinstance(requirements, dict)
        or requirements.get("zero_copy") is not True
        or requirements.get("exact_version_required") is not True
    ):
        raise W09DispatchError("job spec does not require zero-copy exact-version data")
    safety = spec.get("safety")
    required_false = (
        "s3_write",
        "production_mutation",
        "trading_credentials",
        "orders",
        "external_paid_api",
    )
    if not isinstance(safety, dict) or any(safety.get(name) is not False for name in required_false):
        raise W09DispatchError("job spec safety boundary is incomplete")
    if safety.get("w09_isolated_execution") is not True:
        raise W09DispatchError("job spec does not bind isolated W09 execution")

    plan = files["PLAN.md"]
    if spec.get("plan_sha256") != plan.sha256 or spec.get("plan_bytes") != len(plan.payload):
        raise W09DispatchError("job spec drifted from the exact plan")
    if (
        request.get("schema_version") != SCHEMA_REQUEST
        or request.get("job_id") != job_id
        or request.get("plan_sha256") != plan.sha256
        or request.get("plan_bytes") != len(plan.payload)
    ):
        raise W09DispatchError("job request does not bind the exact job and plan")
    if (
        request.get("automatic_execution_requested") is not True
        or request.get("execution_target") != "W09_ISOLATED_RESEARCH"
        or request.get("data_access") != "ZERO_COPY_EXACT_VERSION_READONLY"
        or request.get("arbitrary_plan_code_execution") is not False
    ):
        raise W09DispatchError("job request does not authorize the fixed W09 read-only path")
    if (
        status_value.get("schema_version") != SCHEMA_STATUS
        or status_value.get("job_id") != job_id
        or status_value.get("state") != "QUEUED"
        or status_value.get("research_execution_started") is not False
    ):
        raise W09DispatchError("only an unstarted QUEUED job may be dispatched")
    return files


def _assert_unchanged(files: Mapping[str, StableFile]) -> None:
    for name, original in files.items():
        current = _read_stable_file(original.path, "job %s" % name, CONTROL_FILE_LIMITS[name])
        if current.fingerprint != original.fingerprint or current.sha256 != original.sha256:
            raise W09DispatchError("job %s drifted after validation" % name)


def _validate_ssh_key(path: Path) -> Path:
    path = Path(path).expanduser()
    key = _read_stable_file(path, "Mac SSH key", 128 * 1024)
    mode = stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)
    if mode & 0o077:
        raise W09DispatchError("Mac SSH key permissions must exclude group/world access")
    if _PRIVATE_KEY_RE.search(key.payload) is None:
        raise W09DispatchError("Mac SSH key is not a recognized private-key file")
    return path


def _bundle_manifest(job_id: str, files: Mapping[str, StableFile]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_BUNDLE,
        "job_id": job_id,
        "files": [
            {
                "name": name,
                "bytes": len(files[name].payload),
                "sha256": files[name].sha256,
            }
            for name in CONTROL_FILES
        ],
        "data_files_transferred": 0,
        "data_access": W09_INTERFACE.data_plane,
    }


def _safe_remote_tokens(values: Sequence[str]) -> None:
    if any(SAFE_REMOTE_TOKEN_RE.fullmatch(value) is None for value in values):
        raise W09DispatchError("unsafe token in fixed remote invocation")


def _transport_options(ssh_key: Path) -> list[str]:
    return [
        "-i",
        str(ssh_key),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ConnectTimeout=10",
    ]


def _ssh_command(ssh_key: Path, action: str, job_id: str, *extra: str) -> list[str]:
    remote = [
        str(W09_INTERFACE.control_helper),
        action,
        "--protocol",
        W09_INTERFACE.schema_version,
        "--job-id",
        _valid_job_id(job_id),
        *extra,
    ]
    _safe_remote_tokens(remote)
    return [
        "ssh",
        "-T",
        *_transport_options(ssh_key),
        "--",
        W09_INTERFACE.ssh_target,
        *remote,
    ]


def _scp_command(ssh_key: Path, sources: Sequence[Path], job_id: str) -> list[str]:
    _valid_job_id(job_id)
    if len(sources) != len(CONTROL_FILES):
        raise W09DispatchError("upload must contain exactly four control files")
    destination = "%s:%s/%s/" % (
        W09_INTERFACE.ssh_target,
        W09_INTERFACE.incoming_root,
        job_id,
    )
    return ["scp", *_transport_options(ssh_key), "--", *map(str, sources), destination]


def _run(
    runner: Runner,
    command: Sequence[str],
    *,
    phase: str,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    try:
        result = runner(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise W09DispatchError("W09 %s transport failed" % phase) from exc
    if not isinstance(result.returncode, int) or result.returncode != 0:
        # Remote stderr is intentionally not reflected: it is not trusted and
        # may contain environment detail.  The phase and rc are enough to act.
        raise W09DispatchError(
            "W09 %s transport returned rc=%s" % (phase, result.returncode)
        )
    return result


@contextmanager
def _job_lock(job_dir: Path) -> Iterator[None]:
    lock = job_dir / ".w09-dispatch.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise W09DispatchError("another W09 dispatch operation owns this job") from exc
    try:
        os.write(fd, ("pid=%d\n" % os.getpid()).encode("ascii"))
        os.fsync(fd)
        yield
    finally:
        os.close(fd)
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def _write_snapshot(stage: Path, files: Mapping[str, StableFile]) -> None:
    for name in CONTROL_FILES:
        destination = stage / name
        with destination.open("xb") as handle:
            handle.write(files[name].payload)
            handle.flush()
            os.fsync(handle.fileno())
        destination.chmod(0o400)
    directory = os.open(stage, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def dispatch_job(
    inbox_root: Path,
    job_id: str,
    *,
    ssh_key: Path = DEFAULT_SSH_KEY,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Atomically commit four control files to W09 without starting research."""
    jobs, job_dir = _safe_job_dir(inbox_root, job_id)
    ssh_key = _validate_ssh_key(ssh_key)
    with _job_lock(job_dir):
        files = _read_control_files(job_dir, job_id)
        manifest = _bundle_manifest(job_id, files)
        bundle_sha = hashlib.sha256(_canonical_json_bytes(manifest)).hexdigest()
        stage = Path(tempfile.mkdtemp(prefix=".%s.dispatch." % job_id, dir=jobs))
        stage.chmod(0o700)
        prepared = False
        committed = False
        try:
            _write_snapshot(stage, files)
            _assert_unchanged(files)
            _run(
                runner,
                _ssh_command(
                    ssh_key,
                    "receive-prepare",
                    job_id,
                    "--bundle-sha256",
                    bundle_sha,
                ),
                phase="prepare",
            )
            prepared = True
            _assert_unchanged(files)
            _run(
                runner,
                _scp_command(ssh_key, [stage / name for name in CONTROL_FILES], job_id),
                phase="upload",
                timeout=300,
            )
            _assert_unchanged(files)
            _run(
                runner,
                _ssh_command(
                    ssh_key,
                    "receive-commit",
                    job_id,
                    "--bundle-sha256",
                    bundle_sha,
                ),
                phase="commit",
            )
            committed = True
            _assert_unchanged(files)
        except Exception:
            if prepared and not committed:
                try:
                    _run(
                        runner,
                        _ssh_command(
                            ssh_key,
                            "receive-abort",
                            job_id,
                            "--bundle-sha256",
                            bundle_sha,
                        ),
                        phase="abort",
                    )
                except W09DispatchError:
                    pass
            raise
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    return {
        "schema_version": "research-w09-dispatch-receipt-v1",
        "state": "COMMITTED_NOT_STARTED",
        "job_id": job_id,
        "bundle_sha256": bundle_sha,
        "control_manifest": manifest,
        "ssh_target": W09_INTERFACE.ssh_target,
        "remote_job_root": str(W09_INTERFACE.jobs_root / job_id),
        "worker_unit_template": W09_INTERFACE.worker_unit_template,
        "research_started": False,
        "data_files_transferred": 0,
        "data_access": W09_INTERFACE.data_plane,
        "next_action": "EXPLICIT_START",
    }


def start_job(
    inbox_root: Path,
    job_id: str,
    *,
    bundle_sha256: str,
    ssh_key: Path = DEFAULT_SSH_KEY,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Explicitly ask the fixed W09 helper to start the committed generic job."""
    _, job_dir = _safe_job_dir(inbox_root, job_id)
    if not isinstance(bundle_sha256, str) or SHA256_RE.fullmatch(bundle_sha256) is None:
        raise W09DispatchError("start requires the exact committed bundle SHA-256")
    ssh_key = _validate_ssh_key(ssh_key)
    with _job_lock(job_dir):
        files = _read_control_files(job_dir, job_id)
        expected = hashlib.sha256(
            _canonical_json_bytes(_bundle_manifest(job_id, files))
        ).hexdigest()
        if expected != bundle_sha256:
            raise W09DispatchError("start bundle SHA-256 differs from local immutable job")
        _assert_unchanged(files)
        _run(
            runner,
            _ssh_command(
                ssh_key,
                "start",
                job_id,
                "--bundle-sha256",
                bundle_sha256,
            ),
            phase="explicit-start",
        )
        _assert_unchanged(files)
    return {
        "schema_version": "research-w09-start-receipt-v1",
        "state": "START_REQUEST_ACCEPTED",
        "job_id": job_id,
        "bundle_sha256": bundle_sha256,
        "worker_unit_template": W09_INTERFACE.worker_unit_template,
        "data_access": W09_INTERFACE.data_plane,
    }


def _safe_export_path(name: str, *, directory: bool) -> PurePosixPath:
    if not isinstance(name, str) or not name or "\\" in name:
        raise W09DispatchError("remote export contains an unsafe path")
    clean = name[:-1] if directory and name.endswith("/") else name
    path = PurePosixPath(clean)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise W09DispatchError("remote export contains path traversal")
    if any(SAFE_REPORT_COMPONENT_RE.fullmatch(part) is None for part in path.parts):
        raise W09DispatchError("remote export contains an invalid path component")
    if len(path.parts) > MAX_REPORT_DEPTH:
        raise W09DispatchError("remote export report path is too deep")
    if path.parts[0] not in {"STATUS.json", "RESULTS.json", "REPORT"}:
        raise W09DispatchError("remote export contains an unapproved artifact")
    if path.parts[0] in {"STATUS.json", "RESULTS.json"} and len(path.parts) != 1:
        raise W09DispatchError("remote export has an invalid top-level artifact path")
    if path.parts[0] == "REPORT" and len(path.parts) == 1 and not directory:
        raise W09DispatchError("REPORT must be a directory")
    return path


def _decode_export(payload: bytes, job_id: str) -> dict[PurePosixPath, bytes]:
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_EXPORT_BYTES:
        raise W09DispatchError("remote output export is empty or exceeds 64 MiB")
    files: dict[PurePosixPath, bytes] = {}
    names: set[str] = set()
    total = 0
    try:
        archive = tarfile.open(fileobj=io.BytesIO(payload), mode="r:")
    except tarfile.TarError as exc:
        raise W09DispatchError("remote output is not the fixed tar format") from exc
    with archive:
        members = archive.getmembers()
        if len(members) > MAX_EXPORT_FILES + 64:
            raise W09DispatchError("remote output contains too many tar entries")
        for member in members:
            if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                raise W09DispatchError("remote output contains a link or special file")
            if not (member.isdir() or member.isfile()):
                raise W09DispatchError("remote output contains an unsupported tar entry")
            path = _safe_export_path(member.name, directory=member.isdir())
            folded = str(path).casefold()
            if folded in names:
                raise W09DispatchError("remote output contains duplicate paths")
            names.add(folded)
            if member.isdir():
                continue
            if member.size < 0 or member.size > MAX_EXPORT_FILE_BYTES:
                raise W09DispatchError("remote output artifact exceeds its size limit")
            handle = archive.extractfile(member)
            if handle is None:
                raise W09DispatchError("remote output artifact cannot be read")
            value = handle.read(member.size + 1)
            if len(value) != member.size:
                raise W09DispatchError("remote output artifact size drifted")
            total += len(value)
            if total > MAX_EXPORT_BYTES:
                raise W09DispatchError("remote output artifacts exceed 64 MiB")
            if path.suffix.lower() in {".json", ".html", ".css", ".csv", ".txt", ".md", ".svg"}:
                if _has_credential(value):
                    raise W09DispatchError("remote output contains credential-like content")
            files[path] = value

    status_raw = files.get(PurePosixPath("STATUS.json"))
    if status_raw is None:
        raise W09DispatchError("remote output is missing STATUS.json")
    status_value = _json_object(status_raw, "remote STATUS.json")
    if (
        status_value.get("schema_version") != SCHEMA_STATUS
        or status_value.get("job_id") != job_id
        or status_value.get("state") not in STATUS_STATES
    ):
        raise W09DispatchError("remote STATUS.json does not bind this job")
    results_raw = files.get(PurePosixPath("RESULTS.json"))
    report_files = [path for path in files if path.parts[0] == "REPORT"]
    if status_value.get("state") == "COMPLETE":
        if (
            status_value.get("research_execution_started") is not True
            or status_value.get("report_ready") is not True
            or results_raw is None
            or PurePosixPath("REPORT/index.html") not in files
            or PurePosixPath("REPORT/REPORT_RECEIPT.json") not in files
        ):
            raise W09DispatchError("COMPLETE remote status lacks its result/report bundle")
    elif results_raw is not None or report_files:
        raise W09DispatchError("non-COMPLETE remote status exposes partial result artifacts")

    if results_raw is not None:
        results = _json_object(results_raw, "remote RESULTS.json")
        if results.get("schema_version") != SCHEMA_RESULTS or results.get("job_id") != job_id:
            raise W09DispatchError("remote RESULTS.json does not bind this generic job")
        receipt = _json_object(
            files[PurePosixPath("REPORT/REPORT_RECEIPT.json")],
            "remote REPORT_RECEIPT.json",
        )
        if (
            receipt.get("job_id") != job_id
            or receipt.get("results_sha256") != hashlib.sha256(results_raw).hexdigest()
            or receipt.get("report_sha256")
            != hashlib.sha256(files[PurePosixPath("REPORT/index.html")]).hexdigest()
        ):
            raise W09DispatchError("remote report receipt does not bind results/report bytes")
    return files


def _validate_status_progress(local_raw: bytes, remote_raw: bytes, job_id: str) -> None:
    local = _json_object(local_raw, "local STATUS.json")
    remote = _json_object(remote_raw, "remote STATUS.json")
    if local.get("job_id") != job_id or remote.get("job_id") != job_id:
        raise W09DispatchError("status job id drift")
    local_state = local.get("state")
    remote_state = remote.get("state")
    if local_state not in STATUS_RANK or remote_state not in STATUS_RANK:
        raise W09DispatchError("status contains an unknown state")
    if STATUS_RANK[remote_state] < STATUS_RANK[local_state]:
        raise W09DispatchError("remote status would regress the local job")
    if local_state in TERMINAL_STATES and remote_state != local_state:
        raise W09DispatchError("remote status would replace a terminal local state")


def _write_export_stage(stage: Path, files: Mapping[PurePosixPath, bytes]) -> None:
    for relative, payload in sorted(files.items(), key=lambda item: str(item[0])):
        destination = stage.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        with destination.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        destination.chmod(0o640)


def _tree_hashes(root: Path) -> dict[str, str]:
    if root.is_symlink() or not root.is_dir():
        raise W09DispatchError("local REPORT destination is unsafe")
    values: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise W09DispatchError("local REPORT contains a symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise W09DispatchError("local REPORT contains a special file")
        relative = path.relative_to(root).as_posix()
        item = _read_stable_file(path, "local REPORT artifact", MAX_EXPORT_FILE_BYTES)
        values[relative] = item.sha256
    return values


def _same_file(path: Path, payload: bytes, label: str) -> bool:
    if path.is_symlink():
        raise W09DispatchError("%s destination is a symlink" % label)
    if not path.exists():
        return False
    current = _read_stable_file(path, label, MAX_EXPORT_FILE_BYTES)
    if current.sha256 != hashlib.sha256(payload).hexdigest():
        raise W09DispatchError("%s destination conflicts with remote output" % label)
    return True


def pull_job_outputs(
    inbox_root: Path,
    job_id: str,
    *,
    ssh_key: Path = DEFAULT_SSH_KEY,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Read a coherent STATUS/RESULTS/REPORT snapshot and install it locally."""
    jobs, job_dir = _safe_job_dir(inbox_root, job_id)
    ssh_key = _validate_ssh_key(ssh_key)
    with _job_lock(job_dir):
        local_status = _read_stable_file(
            job_dir / "STATUS.json", "local STATUS.json", CONTROL_FILE_LIMITS["STATUS.json"]
        )
        result = _run(
            runner,
            _ssh_command(
                ssh_key,
                "export",
                job_id,
                "--format",
                W09_INTERFACE.export_format,
                "--artifact-set",
                "STATUS_RESULTS_REPORT",
            ),
            phase="read-only-export",
            timeout=300,
        )
        if not isinstance(result.stdout, bytes):
            raise W09DispatchError("remote output export was not returned as bytes")
        files = _decode_export(result.stdout, job_id)
        remote_status = files[PurePosixPath("STATUS.json")]
        _validate_status_progress(local_status.payload, remote_status, job_id)

        # Refuse a local STATUS change that raced the read-only remote export.
        current_status = _read_stable_file(
            job_dir / "STATUS.json", "local STATUS.json", CONTROL_FILE_LIMITS["STATUS.json"]
        )
        if (
            current_status.fingerprint != local_status.fingerprint
            or current_status.sha256 != local_status.sha256
        ):
            raise W09DispatchError("local STATUS.json drifted during remote export")

        stage = Path(tempfile.mkdtemp(prefix=".%s.pull." % job_id, dir=jobs))
        stage.chmod(0o700)
        installed: list[str] = []
        verified_existing: list[str] = []
        try:
            _write_export_stage(stage, files)
            results_raw = files.get(PurePosixPath("RESULTS.json"))
            report_present = PurePosixPath("REPORT/index.html") in files
            results_existing = False
            report_existing = False
            if results_raw is not None:
                results_existing = _same_file(job_dir / "RESULTS.json", results_raw, "RESULTS.json")
            report_destination = job_dir / "REPORT"
            if report_destination.is_symlink():
                raise W09DispatchError("REPORT destination is a symlink")
            if report_present and report_destination.exists():
                expected = _tree_hashes(stage / "REPORT")
                if _tree_hashes(report_destination) != expected:
                    raise W09DispatchError("REPORT destination conflicts with remote output")
                report_existing = True

            # Recheck immediately before mutation.  Results/report land first;
            # STATUS lands last, so COMPLETE never points at missing artifacts.
            latest_status = _read_stable_file(
                job_dir / "STATUS.json", "local STATUS.json", CONTROL_FILE_LIMITS["STATUS.json"]
            )
            if latest_status.sha256 != local_status.sha256:
                raise W09DispatchError("local STATUS.json drifted before atomic install")
            if results_raw is not None:
                if results_existing:
                    verified_existing.append("RESULTS.json")
                else:
                    os.replace(stage / "RESULTS.json", job_dir / "RESULTS.json")
                    installed.append("RESULTS.json")
            if report_present:
                if report_existing:
                    verified_existing.append("REPORT")
                else:
                    os.replace(stage / "REPORT", report_destination)
                    installed.append("REPORT")
            os.replace(stage / "STATUS.json", job_dir / "STATUS.json")
            installed.append("STATUS.json")
            directory = os.open(job_dir, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    status_value = _json_object(remote_status, "remote STATUS.json")
    return {
        "schema_version": "research-w09-pull-receipt-v1",
        "state": "OUTPUT_SNAPSHOT_INSTALLED",
        "job_id": job_id,
        "remote_state": status_value["state"],
        "remote_job_root": str(W09_INTERFACE.jobs_root / job_id),
        "installed": installed,
        "verified_existing": verified_existing,
        "artifact_sha256": {
            str(path): hashlib.sha256(payload).hexdigest()
            for path, payload in sorted(files.items(), key=lambda item: str(item[0]))
        },
        "data_files_transferred": 0,
        "data_access": W09_INTERFACE.data_plane,
    }


def _interface_json() -> dict[str, Any]:
    return {
        "schema_version": W09_INTERFACE.schema_version,
        "ssh_target": W09_INTERFACE.ssh_target,
        "jobs_root": str(W09_INTERFACE.jobs_root),
        "incoming_root": str(W09_INTERFACE.incoming_root),
        "control_helper": str(W09_INTERFACE.control_helper),
        "worker_unit_template": W09_INTERFACE.worker_unit_template,
        "export_format": W09_INTERFACE.export_format,
        "data_plane": W09_INTERFACE.data_plane,
        "upload_files": list(CONTROL_FILES),
        "dispatch_starts_research": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("dispatch", "start", "pull"):
        command = subparsers.add_parser(name)
        command.add_argument("--inbox-root", required=True, type=Path)
        command.add_argument("--job-id", required=True)
        command.add_argument("--ssh-key", type=Path, default=DEFAULT_SSH_KEY)
        if name == "start":
            command.add_argument("--bundle-sha256", required=True)
    subparsers.add_parser("interface")
    args = parser.parse_args(argv)
    try:
        if args.command == "dispatch":
            receipt = dispatch_job(
                args.inbox_root, args.job_id, ssh_key=args.ssh_key
            )
        elif args.command == "start":
            receipt = start_job(
                args.inbox_root,
                args.job_id,
                bundle_sha256=args.bundle_sha256,
                ssh_key=args.ssh_key,
            )
        elif args.command == "pull":
            receipt = pull_job_outputs(
                args.inbox_root, args.job_id, ssh_key=args.ssh_key
            )
        else:
            receipt = _interface_json()
    except W09DispatchError as exc:
        print("W09_RESEARCH_DISPATCH_REFUSED: %s" % exc)
        return 2
    print(json.dumps(receipt, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
