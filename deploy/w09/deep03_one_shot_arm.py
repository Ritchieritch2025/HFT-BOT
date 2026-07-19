#!/usr/bin/env python3
"""Root-owned one-shot claim state for one exact W09 Deep03 ARM.

The operator-signed ARM remains immutable so its SHA-256 stays auditable.  A
root-owned sidecar, named by that ARM's SHA-256, is the mutable execution
state.  It is atomically created as ``ACTIVE`` once and atomically replaced by
``CONSUMED`` when systemd finishes, regardless of the service outcome.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from typing import Any


CLAIM_SCHEMA = "deep03-w09-one-shot-arm-claim-v2"
AUTHORITY_SCHEMA = "deep03-w09-execution-authority-v1"
ARM_SCHEMA = "deep03-w09-execution-arm-v1"
SERVICE_UNIT = "w09-exploratory-autoresearch.service"
SERVICE_CGROUP = "/system.slice/w09-exploratory-autoresearch.service"
RELEASE_ID = "D3-W2A-2026-07-18.04"
DEFAULT_PROC_CGROUP_PATH = Path("/proc/self/cgroup")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
INVOCATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_CLAIM_BYTES = 64 * 1024


class OneShotArmError(RuntimeError):
    """The root-owned one-shot ARM state failed closed."""


def _utc_now(now: dt.datetime | None = None) -> str:
    value = now or dt.datetime.now(dt.timezone.utc)
    if value.tzinfo is None or value.utcoffset() != dt.timedelta(0):
        raise OneShotArmError("one-shot state timestamp must be UTC")
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        + b"\n"
    )


def _json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise OneShotArmError("%s is not valid JSON" % label) from exc
    if not isinstance(value, dict):
        raise OneShotArmError("%s root is not an object" % label)
    return value


def _secure_file_bytes(
    path: Path,
    *,
    label: str,
    max_bytes: int,
    expected_owner_uid: int,
) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise OneShotArmError("%s is missing or unsafe" % label) from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise OneShotArmError("%s is not a regular file" % label)
        if metadata.st_uid != expected_owner_uid:
            raise OneShotArmError("%s has the wrong owner" % label)
        if metadata.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            raise OneShotArmError("%s is writable" % label)
        if metadata.st_size <= 0 or metadata.st_size > max_bytes:
            raise OneShotArmError("%s size is outside the accepted bound" % label)
        raw = b""
        while len(raw) <= max_bytes:
            chunk = os.read(fd, min(65536, max_bytes + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        if len(raw) != metadata.st_size:
            raise OneShotArmError("%s changed while being read" % label)
        return raw
    finally:
        os.close(fd)


def load_arm_identity(
    *,
    authority_path: Path,
    arm_path: Path,
    expected_owner_uid: int = 0,
) -> dict[str, str]:
    """Read only the immutable identity needed to claim or consume an ARM."""
    authority_raw = _secure_file_bytes(
        Path(authority_path),
        label="AUTHORITY.json",
        max_bytes=1024 * 1024,
        expected_owner_uid=expected_owner_uid,
    )
    arm_raw = _secure_file_bytes(
        Path(arm_path),
        label="execution arm",
        max_bytes=128 * 1024,
        expected_owner_uid=expected_owner_uid,
    )
    authority = _json(authority_raw, "AUTHORITY.json")
    arm = _json(arm_raw, "execution arm")
    authority_sha = hashlib.sha256(authority_raw).hexdigest()
    arm_sha = hashlib.sha256(arm_raw).hexdigest()
    release_id = authority.get("release_id")
    if (
        authority.get("schema_version") != AUTHORITY_SCHEMA
        or authority.get("state") != "ACTIVE"
        or not isinstance(release_id, str)
        or release_id != RELEASE_ID
    ):
        raise OneShotArmError("authority identity is not an active D3-W2A release")
    if (
        arm.get("schema_version") != ARM_SCHEMA
        or arm.get("state") != "ARMED"
        or arm.get("release_id") != release_id
        or arm.get("authority_sha256") != authority_sha
    ):
        raise OneShotArmError("execution ARM identity differs from authority")
    return {
        "release_id": release_id,
        "authority_sha256": authority_sha,
        "arm_sha256": arm_sha,
    }


def _validate_identity(identity: dict[str, str]) -> None:
    if (
        identity.get("release_id") != RELEASE_ID
        or not isinstance(identity.get("authority_sha256"), str)
        or SHA256_RE.fullmatch(identity["authority_sha256"]) is None
        or not isinstance(identity.get("arm_sha256"), str)
        or SHA256_RE.fullmatch(identity["arm_sha256"]) is None
    ):
        raise OneShotArmError("one-shot identity is invalid")


def _validate_invocation(invocation_id: str, service_unit: str) -> None:
    if not isinstance(invocation_id, str) or INVOCATION_ID_RE.fullmatch(invocation_id) is None:
        raise OneShotArmError("systemd INVOCATION_ID is missing or invalid")
    if service_unit != SERVICE_UNIT:
        raise OneShotArmError("one-shot claim is limited to the fixed W09 service")


def _current_service_cgroup(
    *,
    service_unit: str,
    proc_cgroup_path: Path = DEFAULT_PROC_CGROUP_PATH,
) -> str:
    """Read the kernel-owned cgroup-v2 identity for this exact process.

    ``proc_cgroup_path`` is injectable only through the Python API so offline
    tests can supply a synthetic proc file.  The deployed CLI never exposes a
    path override and therefore always reads ``/proc/self/cgroup``.
    """
    if service_unit != SERVICE_UNIT:
        raise OneShotArmError("one-shot claim is limited to the fixed W09 service")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(Path(proc_cgroup_path), flags)
    except OSError as exc:
        raise OneShotArmError("current process cgroup is missing or unsafe") from exc
    try:
        raw = os.read(fd, 16 * 1024 + 1)
        if not raw or len(raw) > 16 * 1024:
            raise OneShotArmError("current process cgroup is outside the accepted bound")
    finally:
        os.close(fd)
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeError as exc:
        raise OneShotArmError("current process cgroup is not ASCII") from exc
    unified: list[str] = []
    for line in lines:
        fields = line.split(":", 2)
        if len(fields) == 3 and fields[0] == "0" and fields[1] == "":
            unified.append(fields[2])
    if len(unified) != 1:
        raise OneShotArmError("current process has no unique cgroup-v2 identity")
    if unified[0] != SERVICE_CGROUP:
        raise OneShotArmError(
            "current process is outside the fixed W09 service cgroup"
        )
    return unified[0]


def _open_claim_root(
    claim_root: Path, *, expected_owner_uid: int
) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(Path(claim_root), flags)
    except OSError as exc:
        raise OneShotArmError("one-shot claim root is missing or unsafe") from exc
    metadata = os.fstat(fd)
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != expected_owner_uid
        or mode != 0o750
    ):
        os.close(fd)
        raise OneShotArmError("one-shot claim root owner/mode is unsafe")
    return fd


def _claim_name(arm_sha256: str) -> str:
    if not isinstance(arm_sha256, str) or SHA256_RE.fullmatch(arm_sha256) is None:
        raise OneShotArmError("ARM SHA-256 is invalid")
    return arm_sha256 + ".json"


def _read_claim_at(
    root_fd: int,
    name: str,
    *,
    expected_owner_uid: int,
) -> tuple[dict[str, Any], bytes]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(name, flags, dir_fd=root_fd)
    except OSError as exc:
        raise OneShotArmError("one-shot ARM was never claimed") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise OneShotArmError("one-shot claim is not a regular file")
        if metadata.st_uid != expected_owner_uid:
            raise OneShotArmError("one-shot claim has the wrong owner")
        if stat.S_IMODE(metadata.st_mode) != 0o440:
            raise OneShotArmError("one-shot claim mode is not 0440")
        if metadata.st_size <= 0 or metadata.st_size > MAX_CLAIM_BYTES:
            raise OneShotArmError("one-shot claim size is outside the accepted bound")
        raw = b""
        while len(raw) <= MAX_CLAIM_BYTES:
            chunk = os.read(fd, min(65536, MAX_CLAIM_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        if len(raw) != metadata.st_size:
            raise OneShotArmError("one-shot claim changed while being read")
        return _json(raw, "one-shot claim"), raw
    finally:
        os.close(fd)


def _validate_claim_fields(
    claim: dict[str, Any],
    *,
    identity: dict[str, str],
    invocation_id: str,
    service_unit: str,
    service_cgroup: str,
    state: str,
) -> None:
    fixed = {
        "schema_version": CLAIM_SCHEMA,
        "state": state,
        "release_id": identity["release_id"],
        "authority_sha256": identity["authority_sha256"],
        "arm_sha256": identity["arm_sha256"],
        "service_unit": service_unit,
        "service_cgroup": service_cgroup,
        "invocation_id": invocation_id,
    }
    for field, expected in fixed.items():
        if claim.get(field) != expected:
            raise OneShotArmError("one-shot claim field mismatch: %s" % field)


def claim_one_shot(
    *,
    claim_root: Path,
    identity: dict[str, str],
    invocation_id: str,
    service_unit: str = SERVICE_UNIT,
    expected_owner_uid: int = 0,
    now: dt.datetime | None = None,
    proc_cgroup_path: Path = DEFAULT_PROC_CGROUP_PATH,
) -> dict[str, Any]:
    """Atomically claim one exact ARM; an existing state can never be reused."""
    _validate_identity(identity)
    _validate_invocation(invocation_id, service_unit)
    service_cgroup = _current_service_cgroup(
        service_unit=service_unit,
        proc_cgroup_path=proc_cgroup_path,
    )
    payload = {
        "schema_version": CLAIM_SCHEMA,
        "state": "ACTIVE",
        **identity,
        "service_unit": service_unit,
        "service_cgroup": service_cgroup,
        "invocation_id": invocation_id,
        "claimed_at_utc": _utc_now(now),
        "consumed_at_utc": None,
        "service_result": None,
        "exit_code": None,
        "exit_status": None,
    }
    raw = _canonical_bytes(payload)
    name = _claim_name(identity["arm_sha256"])
    root_fd = _open_claim_root(claim_root, expected_owner_uid=expected_owner_uid)
    temporary = ".%s.%d.%s.tmp" % (
        identity["arm_sha256"],
        os.getpid(),
        secrets.token_hex(8),
    )
    temp_fd: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        temp_fd = os.open(temporary, flags, 0o400, dir_fd=root_fd)
        os.fchown(temp_fd, -1, os.fstat(root_fd).st_gid)
        written = 0
        while written < len(raw):
            written += os.write(temp_fd, raw[written:])
        os.fchmod(temp_fd, 0o440)
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = None
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise OneShotArmError("this exact authority/ARM was already claimed or consumed") from exc
        os.unlink(temporary, dir_fd=root_fd)
        os.fsync(root_fd)
        return payload
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        try:
            os.unlink(temporary, dir_fd=root_fd)
        except FileNotFoundError:
            pass
        os.close(root_fd)


def validate_active_claim(
    *,
    claim_root: Path,
    identity: dict[str, str],
    invocation_id: str,
    service_unit: str = SERVICE_UNIT,
    expected_owner_uid: int = 0,
    proc_cgroup_path: Path = DEFAULT_PROC_CGROUP_PATH,
) -> tuple[dict[str, Any], bytes]:
    """Return immutable ACTIVE claim bytes or refuse direct/replayed execution."""
    _validate_identity(identity)
    _validate_invocation(invocation_id, service_unit)
    service_cgroup = _current_service_cgroup(
        service_unit=service_unit,
        proc_cgroup_path=proc_cgroup_path,
    )
    root_fd = _open_claim_root(claim_root, expected_owner_uid=expected_owner_uid)
    try:
        claim, raw = _read_claim_at(
            root_fd,
            _claim_name(identity["arm_sha256"]),
            expected_owner_uid=expected_owner_uid,
        )
    finally:
        os.close(root_fd)
    _validate_claim_fields(
        claim,
        identity=identity,
        invocation_id=invocation_id,
        service_unit=service_unit,
        service_cgroup=service_cgroup,
        state="ACTIVE",
    )
    if (
        claim.get("consumed_at_utc") is not None
        or claim.get("service_result") is not None
        or claim.get("exit_code") is not None
        or claim.get("exit_status") is not None
    ):
        raise OneShotArmError("ACTIVE one-shot claim contains terminal fields")
    return claim, raw


def _bounded_result(value: str | None, label: str) -> str:
    text = str(value or "unknown")
    if not text or len(text) > 128 or any(ord(character) < 0x20 for character in text):
        raise OneShotArmError("%s is invalid" % label)
    return text


def consume_one_shot(
    *,
    claim_root: Path,
    identity: dict[str, str],
    invocation_id: str,
    service_result: str | None,
    exit_code: str | None,
    exit_status: str | None,
    service_unit: str = SERVICE_UNIT,
    expected_owner_uid: int = 0,
    now: dt.datetime | None = None,
    proc_cgroup_path: Path = DEFAULT_PROC_CGROUP_PATH,
) -> dict[str, Any]:
    """Atomically transition an ACTIVE exact ARM claim to CONSUMED."""
    _validate_identity(identity)
    _validate_invocation(invocation_id, service_unit)
    service_cgroup = _current_service_cgroup(
        service_unit=service_unit,
        proc_cgroup_path=proc_cgroup_path,
    )
    name = _claim_name(identity["arm_sha256"])
    root_fd = _open_claim_root(claim_root, expected_owner_uid=expected_owner_uid)
    temporary = ".%s.%d.%s.consume" % (
        identity["arm_sha256"],
        os.getpid(),
        secrets.token_hex(8),
    )
    temp_fd: int | None = None
    try:
        current, _raw = _read_claim_at(
            root_fd, name, expected_owner_uid=expected_owner_uid
        )
        if current.get("state") == "CONSUMED":
            _validate_claim_fields(
                current,
                identity=identity,
                invocation_id=invocation_id,
                service_unit=service_unit,
                service_cgroup=service_cgroup,
                state="CONSUMED",
            )
            return current
        _validate_claim_fields(
            current,
            identity=identity,
            invocation_id=invocation_id,
            service_unit=service_unit,
            service_cgroup=service_cgroup,
            state="ACTIVE",
        )
        consumed = {
            **current,
            "state": "CONSUMED",
            "consumed_at_utc": _utc_now(now),
            "service_result": _bounded_result(service_result, "SERVICE_RESULT"),
            "exit_code": _bounded_result(exit_code, "EXIT_CODE"),
            "exit_status": _bounded_result(exit_status, "EXIT_STATUS"),
        }
        raw = _canonical_bytes(consumed)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        temp_fd = os.open(temporary, flags, 0o400, dir_fd=root_fd)
        os.fchown(temp_fd, -1, os.fstat(root_fd).st_gid)
        written = 0
        while written < len(raw):
            written += os.write(temp_fd, raw[written:])
        os.fchmod(temp_fd, 0o440)
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = None
        os.replace(temporary, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        os.fsync(root_fd)
        return consumed
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        try:
            os.unlink(temporary, dir_fd=root_fd)
        except FileNotFoundError:
            pass
        os.close(root_fd)


def _invocation_id() -> str:
    return os.environ.get("INVOCATION_ID", "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("claim", "consume"):
        command = subparsers.add_parser(action)
        command.add_argument("--authority", required=True, type=Path)
        command.add_argument("--arm-file", required=True, type=Path)
        command.add_argument("--claim-root", required=True, type=Path)
        command.add_argument("--service-unit", default=SERVICE_UNIT)
    args = parser.parse_args(argv)
    try:
        identity = load_arm_identity(
            authority_path=args.authority,
            arm_path=args.arm_file,
        )
        if args.action == "claim":
            result = claim_one_shot(
                claim_root=args.claim_root,
                identity=identity,
                invocation_id=_invocation_id(),
                service_unit=args.service_unit,
            )
        else:
            result = consume_one_shot(
                claim_root=args.claim_root,
                identity=identity,
                invocation_id=_invocation_id(),
                service_unit=args.service_unit,
                service_result=os.environ.get("SERVICE_RESULT"),
                exit_code=os.environ.get("EXIT_CODE"),
                exit_status=os.environ.get("EXIT_STATUS"),
            )
    except OneShotArmError as exc:
        print("D3_W2A_ONE_SHOT_ARM_REFUSED: %s" % exc, file=sys.stderr)
        return 2
    print(
        "D3_W2A_ONE_SHOT_ARM_%s release=%s arm_sha256=%s"
        % (result["state"], result["release_id"], result["arm_sha256"])
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
