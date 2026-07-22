#!/usr/bin/env python3
"""Fail-closed, one-shot execution gate for the W09 C1 kill test.

This module deliberately does not create execution authority.  An operator
installs one immutable AUTHORITY document and one narrower immutable ARM as
root-owned 0444 files.  ``validate`` checks every bound byte; ``consume``
additionally creates an O_EXCL receipt before any research computation starts.
"""

from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any, Mapping


AUTHORITY_SCHEMA = "c1-w09-execution-authority-v2"
ARM_SCHEMA = "c1-w09-execution-arm-v2"
CONSUMPTION_SCHEMA = "c1-w09-arm-consumption-v2"
APPROVAL_SCHEMA = "c1-operator-approval-v1"

INSTANCE_ID = "i-0e53d134dceffe166"
INSTANCE_TYPE = "r8g.2xlarge"
ROLE = "w09-research-runner"
EXACT_ELIGIBLE_DATES = ["2026-07-12", "2026-07-15", "2026-07-17"]
CHECKPOINT_BINDING = (
    "c08083fe0f6a05cb60f95f1eee70d61a8075792e83c8f72c62927254c36b1979"
)
CHECKPOINT_ROOT = Path("/srv/w09-research/checkpoints/source-" + CHECKPOINT_BINDING)
CHECKPOINT_STAGES = (
    "l2_availability",
    "l2_replay",
    "l2_episodes",
    "trades_market",
    "dim_market_date",
)
CHECKPOINT_MANIFEST_PATHS = {
    stage: CHECKPOINT_ROOT / stage / "MANIFEST.json" for stage in CHECKPOINT_STAGES
}
UPSTREAM_ROOT = Path(
    "/srv/w09-research/runs/"
    "mode1-20260710-20260717-3cde714ed188-38f8763d13c0-a1"
)
UPSTREAM_PATHS = {
    "input_manifest": UPSTREAM_ROOT / "INPUT_MANIFEST.json",
    "fullscope_l2_execution_receipt": UPSTREAM_ROOT
    / "FULLSCOPE_L2_EXECUTION_RECEIPT.json",
}

AUTHORITY_PATH = Path("/etc/w09/c1/AUTHORITY.json")
ARM_PATH = Path("/etc/w09/c1/EXECUTION_ARM.json")
APPROVAL_TEXT_PATH = Path("/etc/w09/c1/OPERATOR_APPROVAL.txt")
APPROVAL_SIGNATURE_PATH = Path("/etc/w09/c1/OPERATOR_APPROVAL.txt.sig")
SPEC_PATH = Path("/etc/w09/c1/C1_SPEC.md")
CONFIG_PATH = Path("/etc/w09/c1/C1_CONFIG.json")
RUNTIME_COMMIT_PATH = Path("/opt/w09/research/c1-release-commit.txt")
RUNTIME_ROOT = Path("/opt/w09/research")
RUNTIME_FILE_PATHS = {
    "c1_real_fill_runner.py": RUNTIME_ROOT / "tools/research/c1_real_fill_runner.py",
    "c1_fill_kernel.py": RUNTIME_ROOT / "tools/research/c1_fill_kernel.py",
    "c1_checkpoint_reader.py": RUNTIME_ROOT / "tools/research/c1_checkpoint_reader.py",
    "c1_authority_gate.py": RUNTIME_ROOT / "deploy/w09/c1_authority_gate.py",
    "c1_run_once.sh": Path("/usr/local/sbin/c1-run-once"),
}
RUNTIME_FILE_MODES = {
    "c1_real_fill_runner.py": 0o444,
    "c1_fill_kernel.py": 0o444,
    "c1_checkpoint_reader.py": 0o444,
    "c1_authority_gate.py": 0o555,
    "c1_run_once.sh": 0o555,
}
WRITE_ROOT = Path("/srv/w09-research/c1-runs")
RUN_PREFIX = "c1-"
RECEIPT_ROOT = Path("/var/lib/w09-c1/one-shot")
SSH_KEYGEN_PATH = Path("/usr/bin/ssh-keygen")
SSH_KEYGEN_SHA256 = "621136662bb8552f45bdec675fa676d47d2ba9f258574bc58efeccb813b14bc0"
OPERATOR_SIGNER_IDENTITY = "c1-operator-offline-2026-07-22"
OPERATOR_SIGNATURE_NAMESPACE = "hft-bot-c1-operator"
OPERATOR_PUBLIC_KEY_FINGERPRINT = "SHA256:AAE/pGNCTrbM2c+kj7CG4QDggGES+YeejuW5Hn593Dg"
OPERATOR_ALLOWED_SIGNERS_LINE = (
    "c1-operator-offline-2026-07-22 "
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICoRvqFV/ADwfnFJfzor5pPMKOgyPzacD/xqr5RKHqWm"
)

READ_ROOTS = [
    str(Path("/etc/w09/c1")),
    str(RUNTIME_ROOT),
    str(CHECKPOINT_ROOT),
    str(UPSTREAM_ROOT),
]
MAX_RUNTIME_SECONDS = 4 * 60 * 60
MAX_COST_USD = Decimal("3")
W09_USD_PER_HOUR = Decimal("0.50918")
W09_PREFLIGHT_RUNTIME = {
    "evidence_state": "WRAPPER_REVERIFICATION_REQUIRED_BEFORE_ARM_CONSUMPTION",
    "python_version": "3.12.3",
    "python_binary_path": "/usr/bin/python3.12",
    "python_binary_sha256": "a7d56a8a764faf7bbf5c164055a48fd072be52287bdeb523a9e07b2042f4e7e1",
    "duckdb_version": "1.4.5",
    "duckdb_extension_path": (
        "/opt/w09/venv/lib/python3.12/site-packages/"
        "_duckdb.cpython-312-aarch64-linux-gnu.so"
    ),
    "duckdb_extension_sha256": (
        "184620a897f5c1b3dddfa217fe22cd98d614489d8e6bdbfa5fa0b86388af669a"
    ),
    "verification_contract": (
        "c1_run_once.sh verifies resolved Python, exact binary hashes and "
        "imported DuckDB version/path before calling gate consume"
    ),
}
MAX_AUTHORITY_LIFETIME = dt.timedelta(hours=24)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
RUN_ID_RE = re.compile(r"^c1-[a-z0-9][a-z0-9._-]{0,95}$")

FALSE_FIELDS = (
    "live_order_permission",
    "production_mutation",
    "s3_write_permission",
    "rfq_included",
)
AUTHORITY_FIELDS = {
    "schema_version",
    "state",
    "release_id",
    "authorized_instance_id",
    "authorized_instance_type",
    "authorized_role",
    "operator_authorization_text",
    "operator_authorization_sha256",
    "operator_approval_signature_sha256",
    "operator_signer_identity",
    "operator_signature_namespace",
    "operator_public_key_fingerprint",
    "authorized_run_id",
    "authorized_arm_nonce",
    "authorized_arm_not_before_utc",
    "authorized_arm_expires_at_utc",
    "exact_git_commit",
    "runtime_file_sha256s",
    "upstream_sha256s",
    "spec_path",
    "spec_sha256",
    "config_path",
    "config_sha256",
    "checkpoint_root",
    "checkpoint_manifest_sha256s",
    "eligible_dates",
    "authorized_read_roots",
    "authorized_write_root",
    "run_directory_prefix",
    "max_runtime_seconds",
    "cost_cap_usd",
    "live_order_permission",
    "production_mutation",
    "s3_write_permission",
    "rfq_included",
    "issued_at_utc",
    "expires_at_utc",
}
ARM_FIELDS = {
    "schema_version",
    "state",
    "release_id",
    "authority_sha256",
    "exact_git_commit",
    "run_id",
    "arm_nonce",
    "armed_at_utc",
    "not_before_utc",
    "expires_at_utc",
}
APPROVAL_FIELDS = {
    "schema_version",
    "decision",
    "release_id",
    "run_id",
    "arm_nonce",
    "authority_issued_at_utc",
    "authority_expires_at_utc",
    "arm_not_before_utc",
    "arm_expires_at_utc",
    "authorized_instance_id",
    "authorized_instance_type",
    "authorized_role",
    "exact_git_commit",
    "runtime_file_sha256s",
    "upstream_sha256s",
    "spec_sha256",
    "config_sha256",
    "checkpoint_root",
    "checkpoint_manifest_sha256s",
    "eligible_dates",
    "authorized_read_roots",
    "authorized_write_root",
    "max_runtime_seconds",
    "cost_cap_usd",
    "live_order_permission",
    "production_mutation",
    "s3_write_permission",
    "rfq_included",
}


class C1AuthorityError(RuntimeError):
    """The C1 authority, ARM, immutable input, or one-shot state is unsafe."""


def _canonical_json(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise C1AuthorityError("receipt is not canonical-JSON compatible") from exc


def _json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise C1AuthorityError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise C1AuthorityError(f"{label} root is not an object")
    return value


def _utc(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise C1AuthorityError(f"{label} is missing")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise C1AuthorityError(f"{label} is not ISO-8601 UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise C1AuthorityError(f"{label} is not UTC")
    return parsed


def _secure_bytes(
    path: Path,
    *,
    label: str,
    max_bytes: int,
    expected_owner_uid: int,
    exact_mode: int | None = 0o444,
    require_group_read: bool = False,
) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(Path(path), flags)
    except OSError as exc:
        raise C1AuthorityError(f"{label} is missing or unsafe") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise C1AuthorityError(f"{label} is not a regular file")
        if metadata.st_uid != expected_owner_uid:
            raise C1AuthorityError(f"{label} has the wrong owner")
        mode = stat.S_IMODE(metadata.st_mode)
        if exact_mode is not None and mode != exact_mode:
            raise C1AuthorityError(f"{label} mode is not exactly {exact_mode:04o}")
        if exact_mode is None and mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise C1AuthorityError(f"{label} is writable outside its owning identity")
        if require_group_read and not mode & stat.S_IRGRP:
            raise C1AuthorityError(f"{label} is not readable by the checkpoint group")
        if metadata.st_size <= 0 or metadata.st_size > max_bytes:
            raise C1AuthorityError(f"{label} size is outside the accepted bound")
        raw = b""
        while len(raw) <= max_bytes:
            chunk = os.read(fd, min(65536, max_bytes + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        if len(raw) != metadata.st_size:
            raise C1AuthorityError(f"{label} changed while being read")
        return raw
    finally:
        os.close(fd)


def _secure_directory(
    path: Path, *, label: str, expected_owner_uid: int, exact_mode: int
) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(Path(path), flags)
    except OSError as exc:
        raise C1AuthorityError(f"{label} is missing or unsafe") from exc
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != expected_owner_uid
            or stat.S_IMODE(metadata.st_mode) != exact_mode
        ):
            raise C1AuthorityError(
                f"{label} owner/mode must be uid={expected_owner_uid} {exact_mode:04o}"
            )
    finally:
        os.close(fd)


def _write_exclusive_receipt(
    path: Path,
    payload: Mapping[str, Any],
    *,
    expected_owner_uid: int,
) -> None:
    """Write one root-controlled canonical receipt; never replace a prior file."""
    _secure_directory(
        Path(path).parent,
        label="control receipt directory",
        expected_owner_uid=expected_owner_uid,
        exact_mode=0o750,
    )
    raw = _canonical_json(dict(payload))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(Path(path), flags, 0o400)
    except OSError as exc:
        raise C1AuthorityError("control receipt already exists or is unsafe") from exc
    try:
        written = 0
        while written < len(raw):
            written += os.write(fd, raw[written:])
        os.fchmod(fd, 0o444)
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise C1AuthorityError(f"{label} is not a lowercase SHA-256")
    return value


def _exact_sha(raw: bytes, expected: Any, label: str) -> str:
    digest = _sha(expected, label)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise C1AuthorityError(f"{label} differs from installed bytes")
    return digest


def _commit(raw: bytes) -> str:
    try:
        value = raw.decode("ascii").strip()
    except UnicodeError as exc:
        raise C1AuthorityError("runtime commit is not ASCII") from exc
    if COMMIT_RE.fullmatch(value) is None:
        raise C1AuthorityError("runtime commit is not one exact 40-character commit")
    return value


def _cost(value: Any) -> Decimal:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or (isinstance(value, float) and not math.isfinite(value))
    ):
        raise C1AuthorityError("cost_cap_usd must be a finite JSON number")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise C1AuthorityError("cost_cap_usd is not a decimal amount") from exc
    if result <= 0 or result > MAX_COST_USD:
        raise C1AuthorityError("cost_cap_usd must be in (0,3]")
    return result


def _strict_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise C1AuthorityError(f"{label} fields differ; missing={missing} extra={extra}")


def _canonical_approval_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                dict(value),
                sort_keys=True,
                indent=2,
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
            + b"\n"
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise C1AuthorityError("operator approval is not canonical JSON") from exc


def _verify_operator_signature(
    approval_raw: bytes,
    signature_raw: bytes,
    *,
    ssh_keygen_path: Path,
    allowed_signers_line: str,
) -> None:
    """Verify an OpenSSH detached signature without writing trusted bytes."""
    if not allowed_signers_line.endswith("\n"):
        allowed_signers_line += "\n"
    handles = [tempfile.TemporaryFile(), tempfile.TemporaryFile()]
    try:
        handles[0].write(allowed_signers_line.encode("ascii"))
        handles[1].write(signature_raw)
        for handle in handles:
            handle.flush()
            handle.seek(0)
        fd_root = "/proc/self/fd" if Path("/proc/self/fd").is_dir() else "/dev/fd"
        command = [
            str(ssh_keygen_path),
            "-Y",
            "verify",
            "-f",
            f"{fd_root}/{handles[0].fileno()}",
            "-I",
            OPERATOR_SIGNER_IDENTITY,
            "-n",
            OPERATOR_SIGNATURE_NAMESPACE,
            "-s",
            f"{fd_root}/{handles[1].fileno()}",
        ]
        completed = subprocess.run(
            command,
            input=approval_raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"LANG": "C", "PATH": "/usr/bin:/bin"},
            pass_fds=tuple(handle.fileno() for handle in handles),
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise C1AuthorityError("operator signature verifier failed closed") from exc
    finally:
        for handle in handles:
            handle.close()
    if completed.returncode != 0:
        raise C1AuthorityError("operator Ed25519 signature verification failed")


def _validate_signed_approval(
    authority: Mapping[str, Any], approval_raw: bytes
) -> dict[str, Any]:
    approval = _json(approval_raw, "operator approval text")
    _strict_fields(approval, APPROVAL_FIELDS, "operator approval")
    if _canonical_approval_bytes(approval) != approval_raw:
        raise C1AuthorityError("operator approval text is not canonical pretty JSON")
    fixed = {
        "schema_version": APPROVAL_SCHEMA,
        "decision": "APPROVE_ONE_SHOT_OFFLINE_C1",
        "release_id": authority.get("release_id"),
        "run_id": authority.get("authorized_run_id"),
        "arm_nonce": authority.get("authorized_arm_nonce"),
        "authority_issued_at_utc": authority.get("issued_at_utc"),
        "authority_expires_at_utc": authority.get("expires_at_utc"),
        "arm_not_before_utc": authority.get("authorized_arm_not_before_utc"),
        "arm_expires_at_utc": authority.get("authorized_arm_expires_at_utc"),
        "authorized_instance_id": INSTANCE_ID,
        "authorized_instance_type": INSTANCE_TYPE,
        "authorized_role": ROLE,
        "exact_git_commit": authority.get("exact_git_commit"),
        "runtime_file_sha256s": authority.get("runtime_file_sha256s"),
        "upstream_sha256s": authority.get("upstream_sha256s"),
        "spec_sha256": authority.get("spec_sha256"),
        "config_sha256": authority.get("config_sha256"),
        "checkpoint_root": str(CHECKPOINT_ROOT),
        "checkpoint_manifest_sha256s": authority.get(
            "checkpoint_manifest_sha256s"
        ),
        "eligible_dates": EXACT_ELIGIBLE_DATES,
        "authorized_read_roots": READ_ROOTS,
        "authorized_write_root": str(WRITE_ROOT),
        "max_runtime_seconds": authority.get("max_runtime_seconds"),
        "cost_cap_usd": authority.get("cost_cap_usd"),
        "live_order_permission": False,
        "production_mutation": False,
        "s3_write_permission": False,
        "rfq_included": False,
    }
    for field, expected in fixed.items():
        if approval.get(field) != expected:
            raise C1AuthorityError(f"signed operator approval mismatch: {field}")
    return approval


def _fixed_authority_fields(authority: Mapping[str, Any]) -> None:
    fixed = {
        "schema_version": AUTHORITY_SCHEMA,
        "state": "ACTIVE",
        "authorized_instance_id": INSTANCE_ID,
        "authorized_instance_type": INSTANCE_TYPE,
        "authorized_role": ROLE,
        "spec_path": str(SPEC_PATH),
        "config_path": str(CONFIG_PATH),
        "checkpoint_root": str(CHECKPOINT_ROOT),
        "eligible_dates": EXACT_ELIGIBLE_DATES,
        "authorized_read_roots": READ_ROOTS,
        "authorized_write_root": str(WRITE_ROOT),
        "run_directory_prefix": RUN_PREFIX,
        "operator_signer_identity": OPERATOR_SIGNER_IDENTITY,
        "operator_signature_namespace": OPERATOR_SIGNATURE_NAMESPACE,
        "operator_public_key_fingerprint": OPERATOR_PUBLIC_KEY_FINGERPRINT,
    }
    for field, expected in fixed.items():
        if authority.get(field) != expected:
            raise C1AuthorityError(f"AUTHORITY field mismatch: {field}")
    release_id = authority.get("release_id")
    if not isinstance(release_id, str) or not RUN_ID_RE.fullmatch(release_id):
        raise C1AuthorityError("release_id must be one bounded c1-* identifier")
    for field in FALSE_FIELDS:
        if authority.get(field) is not False:
            raise C1AuthorityError(f"AUTHORITY field must be explicit false: {field}")
    run_id = authority.get("authorized_run_id")
    if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
        raise C1AuthorityError("authorized_run_id is invalid")
    nonce = authority.get("authorized_arm_nonce")
    if not isinstance(nonce, str) or NONCE_RE.fullmatch(nonce) is None:
        raise C1AuthorityError("authorized_arm_nonce is invalid")
    _utc(
        authority.get("authorized_arm_not_before_utc"),
        "authorized ARM not-before",
    )
    _utc(authority.get("authorized_arm_expires_at_utc"), "authorized ARM expiry")


def validate_bundle(
    *,
    authority_path: Path = AUTHORITY_PATH,
    arm_path: Path = ARM_PATH,
    approval_text_path: Path = APPROVAL_TEXT_PATH,
    approval_signature_path: Path = APPROVAL_SIGNATURE_PATH,
    spec_path: Path = SPEC_PATH,
    config_path: Path = CONFIG_PATH,
    runtime_commit_path: Path = RUNTIME_COMMIT_PATH,
    runtime_file_paths: Mapping[str, Path] = RUNTIME_FILE_PATHS,
    upstream_paths: Mapping[str, Path] = UPSTREAM_PATHS,
    ssh_keygen_path: Path = SSH_KEYGEN_PATH,
    expected_ssh_keygen_sha256: str = SSH_KEYGEN_SHA256,
    allowed_signers_line: str = OPERATOR_ALLOWED_SIGNERS_LINE,
    verifier_owner_uid: int = 0,
    checkpoint_manifest_paths: Mapping[str, Path] = CHECKPOINT_MANIFEST_PATHS,
    expected_owner_uid: int = 0,
    checkpoint_owner_uid: int | None = None,
    now: dt.datetime | None = None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Validate exact immutable inputs and return a bounded execution result."""
    now = now or dt.datetime.now(dt.timezone.utc)
    authority_raw = _secure_bytes(
        authority_path,
        label="AUTHORITY.json",
        max_bytes=1024 * 1024,
        expected_owner_uid=expected_owner_uid,
    )
    arm_raw = _secure_bytes(
        arm_path,
        label="EXECUTION_ARM.json",
        max_bytes=128 * 1024,
        expected_owner_uid=expected_owner_uid,
    )
    approval_raw = _secure_bytes(
        approval_text_path,
        label="external operator approval text",
        max_bytes=1024 * 1024,
        expected_owner_uid=expected_owner_uid,
    )
    approval_signature_raw = _secure_bytes(
        approval_signature_path,
        label="external operator approval signature",
        max_bytes=128 * 1024,
        expected_owner_uid=expected_owner_uid,
    )
    ssh_keygen_raw = _secure_bytes(
        ssh_keygen_path,
        label="pinned ssh-keygen verifier",
        max_bytes=4 * 1024 * 1024,
        expected_owner_uid=verifier_owner_uid,
        exact_mode=0o755,
    )
    _exact_sha(
        ssh_keygen_raw,
        expected_ssh_keygen_sha256,
        "pinned ssh-keygen verifier SHA-256",
    )
    spec_raw = _secure_bytes(
        spec_path,
        label="C1 spec",
        max_bytes=4 * 1024 * 1024,
        expected_owner_uid=expected_owner_uid,
    )
    config_raw = _secure_bytes(
        config_path,
        label="C1 config",
        max_bytes=1024 * 1024,
        expected_owner_uid=expected_owner_uid,
    )
    runtime_raw = _secure_bytes(
        runtime_commit_path,
        label="runtime commit",
        max_bytes=256,
        expected_owner_uid=expected_owner_uid,
    )
    if set(runtime_file_paths) != set(RUNTIME_FILE_PATHS):
        raise C1AuthorityError("runtime file set has missing or extra keys")
    runtime_file_raw = {
        name: _secure_bytes(
            Path(runtime_file_paths[name]),
            label=f"installed runtime {name}",
            max_bytes=16 * 1024 * 1024,
            expected_owner_uid=expected_owner_uid,
            exact_mode=RUNTIME_FILE_MODES[name],
        )
        for name in RUNTIME_FILE_PATHS
    }
    if set(upstream_paths) != set(UPSTREAM_PATHS):
        raise C1AuthorityError("upstream file set has missing or extra keys")
    if set(checkpoint_manifest_paths) != set(CHECKPOINT_STAGES):
        raise C1AuthorityError("exactly five named checkpoint manifests are required")
    if checkpoint_owner_uid is None:
        try:
            checkpoint_owner_uid = pwd.getpwnam("ubuntu").pw_uid
        except KeyError as exc:
            raise C1AuthorityError("ubuntu checkpoint owner identity is unavailable") from exc
    upstream_raw = {
        name: _secure_bytes(
            Path(upstream_paths[name]),
            label=f"fixed upstream {name}",
            max_bytes=16 * 1024 * 1024,
            expected_owner_uid=checkpoint_owner_uid,
            exact_mode=0o640,
            require_group_read=True,
        )
        for name in UPSTREAM_PATHS
    }
    checkpoint_raw = {
        stage: _secure_bytes(
            Path(checkpoint_manifest_paths[stage]),
            label=f"{stage} checkpoint manifest",
            max_bytes=16 * 1024 * 1024,
            expected_owner_uid=checkpoint_owner_uid,
            exact_mode=None,
            require_group_read=True,
        )
        for stage in CHECKPOINT_STAGES
    }

    authority = _json(authority_raw, "AUTHORITY.json")
    arm = _json(arm_raw, "EXECUTION_ARM.json")
    _strict_fields(authority, AUTHORITY_FIELDS, "AUTHORITY")
    _strict_fields(arm, ARM_FIELDS, "ARM")
    _fixed_authority_fields(authority)

    operator_text = authority.get("operator_authorization_text")
    if not isinstance(operator_text, str) or not operator_text.strip():
        raise C1AuthorityError("operator_authorization_text is missing")
    operator_sha = _sha(
        authority.get("operator_authorization_sha256"),
        "operator_authorization_sha256",
    )
    try:
        external_operator_text = approval_raw.decode("utf-8")
    except UnicodeError as exc:
        raise C1AuthorityError("external operator approval is not UTF-8") from exc
    if operator_text != external_operator_text:
        raise C1AuthorityError("AUTHORITY operator text differs from external approval")
    if hashlib.sha256(approval_raw).hexdigest() != operator_sha:
        raise C1AuthorityError("operator authorization text SHA-256 mismatch")
    signature_sha = _exact_sha(
        approval_signature_raw,
        authority.get("operator_approval_signature_sha256"),
        "operator_approval_signature_sha256",
    )
    _verify_operator_signature(
        approval_raw,
        approval_signature_raw,
        ssh_keygen_path=ssh_keygen_path,
        allowed_signers_line=allowed_signers_line,
    )
    signed_approval = _validate_signed_approval(authority, approval_raw)

    exact_commit = authority.get("exact_git_commit")
    if not isinstance(exact_commit, str) or COMMIT_RE.fullmatch(exact_commit) is None:
        raise C1AuthorityError("exact_git_commit is invalid")
    installed_commit = _commit(runtime_raw)
    if exact_commit != installed_commit:
        raise C1AuthorityError("authority commit differs from installed runtime")
    runtime_file_shas = authority.get("runtime_file_sha256s")
    if (
        not isinstance(runtime_file_shas, dict)
        or set(runtime_file_shas) != set(RUNTIME_FILE_PATHS)
    ):
        raise C1AuthorityError("runtime_file_sha256s has missing or extra keys")
    for name in RUNTIME_FILE_PATHS:
        _exact_sha(
            runtime_file_raw[name],
            runtime_file_shas.get(name),
            f"runtime_file_sha256s.{name}",
        )
    upstream_shas = authority.get("upstream_sha256s")
    if not isinstance(upstream_shas, dict) or set(upstream_shas) != set(UPSTREAM_PATHS):
        raise C1AuthorityError("upstream_sha256s has missing or extra keys")
    for name in UPSTREAM_PATHS:
        _exact_sha(
            upstream_raw[name],
            upstream_shas.get(name),
            f"upstream_sha256s.{name}",
        )

    spec_sha = _exact_sha(spec_raw, authority.get("spec_sha256"), "spec_sha256")
    config_sha = _exact_sha(
        config_raw, authority.get("config_sha256"), "config_sha256"
    )
    manifest_shas = authority.get("checkpoint_manifest_sha256s")
    if not isinstance(manifest_shas, dict) or set(manifest_shas) != set(CHECKPOINT_STAGES):
        raise C1AuthorityError("checkpoint_manifest_sha256s must bind exactly five stages")
    for stage in CHECKPOINT_STAGES:
        _exact_sha(
            checkpoint_raw[stage],
            manifest_shas.get(stage),
            f"checkpoint_manifest_sha256s.{stage}",
        )

    runtime_seconds = authority.get("max_runtime_seconds")
    if (
        not isinstance(runtime_seconds, int)
        or isinstance(runtime_seconds, bool)
        or runtime_seconds <= 0
        or runtime_seconds > MAX_RUNTIME_SECONDS
    ):
        raise C1AuthorityError("max_runtime_seconds must be in [1,14400]")
    cost_cap = _cost(authority.get("cost_cap_usd"))
    required_cost = (
        Decimal(runtime_seconds) * W09_USD_PER_HOUR / Decimal(3600)
    )
    if cost_cap < required_cost:
        raise C1AuthorityError("cost cap does not cover the authorized runtime")

    issued = _utc(authority.get("issued_at_utc"), "authority issued_at_utc")
    expires = _utc(authority.get("expires_at_utc"), "authority expires_at_utc")
    if not issued < expires or expires - issued > MAX_AUTHORITY_LIFETIME:
        raise C1AuthorityError("authority lifetime must be positive and no longer than 24h")
    if now < issued or now >= expires:
        raise C1AuthorityError("authority is not active")

    authority_sha = hashlib.sha256(authority_raw).hexdigest()
    arm_fixed = {
        "schema_version": ARM_SCHEMA,
        "state": "ARMED",
        "release_id": authority["release_id"],
        "authority_sha256": authority_sha,
        "exact_git_commit": exact_commit,
    }
    for field, expected in arm_fixed.items():
        if arm.get(field) != expected:
            raise C1AuthorityError(f"ARM field mismatch: {field}")
    run_id = arm.get("run_id")
    if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
        raise C1AuthorityError("ARM run_id must be one bounded c1-* identifier")
    nonce = arm.get("arm_nonce")
    if not isinstance(nonce, str) or NONCE_RE.fullmatch(nonce) is None:
        raise C1AuthorityError("ARM nonce is invalid")
    if run_id != authority["authorized_run_id"] or nonce != authority["authorized_arm_nonce"]:
        raise C1AuthorityError("ARM run_id/nonce differ from signed operator approval")
    armed = _utc(arm.get("armed_at_utc"), "ARM armed_at_utc")
    not_before = _utc(arm.get("not_before_utc"), "ARM not_before_utc")
    arm_expires = _utc(arm.get("expires_at_utc"), "ARM expires_at_utc")
    if (
        arm.get("not_before_utc") != authority["authorized_arm_not_before_utc"]
        or arm.get("expires_at_utc") != authority["authorized_arm_expires_at_utc"]
    ):
        raise C1AuthorityError("ARM window differs from signed operator approval")
    if not (issued <= armed <= not_before < arm_expires <= expires):
        raise C1AuthorityError("ARM window is outside the authority window")
    if arm_expires - not_before >= expires - issued:
        raise C1AuthorityError("ARM window must be strictly narrower than authority")
    if now < not_before or now >= arm_expires:
        raise C1AuthorityError("ARM is not active")
    active_seconds = int((arm_expires - now).total_seconds())
    effective_runtime = min(runtime_seconds, active_seconds)
    if effective_runtime <= 0:
        raise C1AuthorityError("ARM has no executable time remaining")

    result = {
        "schema_version": "c1-w09-authority-gate-pass-v1",
        "state": "AUTHORIZED",
        "release_id": authority["release_id"],
        "run_id": run_id,
        "authority_sha256": authority_sha,
        "arm_sha256": hashlib.sha256(arm_raw).hexdigest(),
        "arm_nonce": nonce,
        "exact_git_commit": exact_commit,
        "runtime_file_sha256s": dict(runtime_file_shas),
        "upstream_sha256s": dict(upstream_shas),
        "spec_sha256": spec_sha,
        "config_sha256": config_sha,
        "checkpoint_manifest_sha256s": dict(manifest_shas),
        "eligible_dates": list(EXACT_ELIGIBLE_DATES),
        "checkpoint_root": str(CHECKPOINT_ROOT),
        "authorized_write_root": str(WRITE_ROOT),
        "run_directory": str(WRITE_ROOT / run_id),
        "control_directory": str(WRITE_ROOT / run_id / "control"),
        "work_directory": str(WRITE_ROOT / run_id / "work"),
        "output_directory": str(WRITE_ROOT / run_id / "work" / "results"),
        "max_runtime_seconds": runtime_seconds,
        "effective_runtime_seconds": effective_runtime,
        "cost_cap_usd": float(cost_cap),
        "required_runtime_cost_usd": float(required_cost),
        "cost_boundary": {
            "state": "OPERATOR_ESTIMATE_ONLY_NOT_AWS_BILLING_ENFORCEMENT",
            "hourly_rate_assumption_usd": float(W09_USD_PER_HOUR),
            "operator_budget_usd": float(cost_cap),
            "estimated_max_instance_runtime_cost_usd": float(required_cost),
            "aws_budgets_api_enforced": False,
            "ebs_network_and_other_charges_included": False,
        },
        "runtime_environment_preflight": dict(W09_PREFLIGHT_RUNTIME),
        "host_producer_toctou_boundary": {
            "eliminated": False,
            "reason": (
                "private read-only mounts constrain C1 but cannot freeze an "
                "independent host ubuntu producer outside the namespace"
            ),
            "mitigation": (
                "exact manifest binding plus checkpoint-reader per-file hash "
                "validation; producer quiescence remains an external precondition"
            ),
        },
        "expires_at_utc": authority["expires_at_utc"],
        "arm_expires_at_utc": arm["expires_at_utc"],
        "operator_authorization_sha256": operator_sha,
        "operator_authorization_text": operator_text,
        "operator_approval_signature_sha256": signature_sha,
        "operator_signer_identity": OPERATOR_SIGNER_IDENTITY,
        "operator_signature_namespace": OPERATOR_SIGNATURE_NAMESPACE,
        "operator_public_key_fingerprint": OPERATOR_PUBLIC_KEY_FINGERPRINT,
        "signed_operator_approval": signed_approval,
        "ssh_keygen_sha256": expected_ssh_keygen_sha256,
    }
    artifacts = {
        "AUTHORITY.json": authority_raw,
        "EXECUTION_ARM.json": arm_raw,
        "OPERATOR_APPROVAL.txt": approval_raw,
        "OPERATOR_APPROVAL.txt.sig": approval_signature_raw,
        "SSH_KEYGEN": ssh_keygen_raw,
        "C1_SPEC.md": spec_raw,
        "C1_CONFIG.json": config_raw,
        "RUNTIME_COMMIT.txt": runtime_raw,
        **{f"runtime/{name}": runtime_file_raw[name] for name in RUNTIME_FILE_PATHS},
        **{f"upstream/{name}": upstream_raw[name] for name in UPSTREAM_PATHS},
        **{f"{stage}/MANIFEST.json": checkpoint_raw[stage] for stage in CHECKPOINT_STAGES},
    }
    return result, artifacts


def consume_one_shot(
    *,
    receipt_root: Path = RECEIPT_ROOT,
    expected_owner_uid: int = 0,
    now: dt.datetime | None = None,
    **validation_kwargs: Any,
) -> dict[str, Any]:
    """Validate and irreversibly consume one exact ARM with O_EXCL."""
    now = now or dt.datetime.now(dt.timezone.utc)
    result, _artifacts = validate_bundle(
        expected_owner_uid=expected_owner_uid, now=now, **validation_kwargs
    )
    _secure_directory(
        receipt_root,
        label="one-shot receipt root",
        expected_owner_uid=expected_owner_uid,
        exact_mode=0o750,
    )
    receipt = {
        "schema_version": CONSUMPTION_SCHEMA,
        "state": "CONSUMED_BEFORE_COMPUTE",
        "release_id": result["release_id"],
        "run_id": result["run_id"],
        "authority_sha256": result["authority_sha256"],
        "arm_sha256": result["arm_sha256"],
        "arm_nonce": result["arm_nonce"],
        "operator_authorization_sha256": result[
            "operator_authorization_sha256"
        ],
        "exact_git_commit": result["exact_git_commit"],
        "consumed_at_utc": now.isoformat().replace("+00:00", "Z"),
        "authorized_max_runtime_seconds": result["max_runtime_seconds"],
        "effective_runtime_seconds": result["effective_runtime_seconds"],
        "execution_deadline_utc": (
            now + dt.timedelta(seconds=result["effective_runtime_seconds"])
        ).isoformat().replace("+00:00", "Z"),
    }
    raw = _canonical_json(receipt)
    # The externally signed approval hash is the stable one-shot identity.
    # Keying by literal ARM bytes alone would permit replay by harmlessly
    # reserializing AUTHORITY/ARM JSON while retaining the same signed scope.
    name = result["operator_authorization_sha256"] + ".json"
    root_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        root_flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        root_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        root_flags |= os.O_NOFOLLOW
    root_fd = os.open(receipt_root, root_flags)
    fd: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(name, flags, 0o400, dir_fd=root_fd)
        except FileExistsError as exc:
            raise C1AuthorityError(
                "this signed operator approval was already consumed"
            ) from exc
        written = 0
        while written < len(raw):
            written += os.write(fd, raw[written:])
        os.fchmod(fd, 0o444)
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.fsync(root_fd)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(root_fd)
    return {**result, "consumption_receipt": str(receipt_root / name)}


def record_control_receipts(
    *,
    receipt_root: Path = RECEIPT_ROOT,
    control_receipt_root: Path | None = None,
    expected_owner_uid: int = 0,
    now: dt.datetime | None = None,
    **validation_kwargs: Any,
) -> dict[str, Any]:
    """Persist auditable run-local receipts after, and only after, consumption."""
    now = now or dt.datetime.now(dt.timezone.utc)
    result, _artifacts = validate_bundle(
        expected_owner_uid=expected_owner_uid,
        now=now,
        **validation_kwargs,
    )
    global_path = Path(receipt_root) / (
        result["operator_authorization_sha256"] + ".json"
    )
    raw = _secure_bytes(
        global_path,
        label="global ARM consumption receipt",
        max_bytes=1024 * 1024,
        expected_owner_uid=expected_owner_uid,
    )
    consumed = _json(raw, "global ARM consumption receipt")
    required = {
        "schema_version": CONSUMPTION_SCHEMA,
        "state": "CONSUMED_BEFORE_COMPUTE",
        "release_id": result["release_id"],
        "run_id": result["run_id"],
        "authority_sha256": result["authority_sha256"],
        "arm_sha256": result["arm_sha256"],
        "arm_nonce": result["arm_nonce"],
        "operator_authorization_sha256": result[
            "operator_authorization_sha256"
        ],
        "exact_git_commit": result["exact_git_commit"],
    }
    for field, expected in required.items():
        if consumed.get(field) != expected:
            raise C1AuthorityError(f"global consumption receipt mismatch: {field}")
    terminal_fields = {
        "consumed_at_utc",
        "authorized_max_runtime_seconds",
        "effective_runtime_seconds",
        "execution_deadline_utc",
    }
    if set(consumed) != {*required, *terminal_fields}:
        raise C1AuthorityError("global consumption receipt fields differ")
    consumed_at = _utc(consumed.get("consumed_at_utc"), "global consumed_at_utc")
    deadline = _utc(
        consumed.get("execution_deadline_utc"), "global execution_deadline_utc"
    )
    consumed_effective = consumed.get("effective_runtime_seconds")
    if (
        type(consumed_effective) is not int
        or consumed_effective <= 0
        or consumed_effective > result["max_runtime_seconds"]
        or consumed.get("authorized_max_runtime_seconds")
        != result["max_runtime_seconds"]
    ):
        raise C1AuthorityError("global consumption runtime binding is invalid")
    if deadline != consumed_at + dt.timedelta(seconds=consumed_effective):
        raise C1AuthorityError("global consumption deadline differs from consumed runtime")
    current = now
    if current < consumed_at or current >= deadline:
        raise C1AuthorityError("global consumption deadline is no longer active")
    if deadline > _utc(result["arm_expires_at_utc"], "result arm expiry"):
        raise C1AuthorityError("global consumption deadline exceeds the ARM")
    remaining_runtime = min(
        consumed_effective, int((deadline - current).total_seconds())
    )
    if remaining_runtime <= 0:
        raise C1AuthorityError("global consumption has no runtime remaining")
    control_receipt_root = control_receipt_root or (
        WRITE_ROOT / str(result["run_id"]) / "control"
    )
    _write_exclusive_receipt(
        Path(control_receipt_root) / "CONTROL_AUTHORIZATION_RECEIPT.json",
        result,
        expected_owner_uid=expected_owner_uid,
    )
    final = {
        **result,
        "consumed_effective_runtime_seconds": consumed_effective,
        "effective_runtime_seconds": remaining_runtime,
        "execution_deadline_utc": consumed["execution_deadline_utc"],
        "consumption_receipt": str(global_path),
    }
    _write_exclusive_receipt(
        Path(control_receipt_root) / "ARM_CONSUMPTION_RECEIPT.json",
        final,
        expected_owner_uid=expected_owner_uid,
    )
    return final


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "consume", "record"))
    args = parser.parse_args(argv)
    try:
        if args.action == "validate":
            result, _artifacts = validate_bundle()
        elif args.action == "consume":
            result = consume_one_shot()
        else:
            result = record_control_receipts()
    except C1AuthorityError as exc:
        print(f"C1_AUTHORITY_REFUSED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
