#!/usr/bin/env python3
"""Fail-closed exact-release gate for W09 D3-W2A execution.

The normal W09 installer never creates any authority, adopted-plan, or arm
file.  A separately audited exact-SHA release must install all three as
root-owned, non-writable files.  The service checks this gate before every
cycle, and the cycle validates it again before selecting or reading data.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any


AUTHORITY_SCHEMA = "deep03-w09-execution-authority-v1"
ARM_SCHEMA = "deep03-w09-execution-arm-v1"
MODE = "MODE 1 / EXPLORATORY_AUTORESEARCH"
WORK_PACKAGE = "D3-W2A"
PHASE = "OPEN_DISCOVERY"
INSTANCE_ID = "i-0e53d134dceffe166"
ROLE = "w09-research-runner"
PLAN_INSTALL_PATH = "/etc/w09/deep03/adopted-plan.md"
WRITE_ROOTS = {
    "/srv/w09-research/cache-v3-exploratory",
    "/srv/w09-research/automation",
    "/srv/w09-research/runs",
}
NETWORK_OPERATIONS = ["S3_READONLY_MANIFEST_AND_EXACT_VERSION"]
RELEASE_RE = re.compile(r"^D3-W2A-[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
V3_RELEASE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})__v3ref__seal-[0-9a-f]{8}"
    r"__pub-[0-9a-f]{16}$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
FALSE_AUTHORITY_FIELDS = (
    "external_account_actions",
    "paid_api_access",
    "credential_use",
    "live_order_permission",
    "production_mutation",
    "push_permission",
    "telegram_send_permission",
    "telegram_update_read_permission",
    "rfq_included",
    "strict_acceptance_claimed",
    "candidate_or_profit_claim",
)


class AuthorityError(RuntimeError):
    """An exact-release or arm binding failed closed."""


def _utc(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise AuthorityError("%s is missing" % label)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthorityError("%s is not ISO-8601 UTC" % label) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise AuthorityError("%s is not UTC" % label)
    return parsed


def _secure_bytes(
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
        raise AuthorityError("%s is missing or unsafe" % label) from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise AuthorityError("%s is not a regular file" % label)
        if metadata.st_uid != expected_owner_uid:
            raise AuthorityError("%s has the wrong owner" % label)
        if metadata.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            raise AuthorityError("%s is writable" % label)
        if metadata.st_size <= 0 or metadata.st_size > max_bytes:
            raise AuthorityError("%s size is outside the accepted bound" % label)
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(max_bytes + 1)
        if len(raw) != metadata.st_size:
            raise AuthorityError("%s changed while being read" % label)
        return raw
    finally:
        os.close(fd)


def _json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise AuthorityError("%s is not valid JSON" % label) from exc
    if not isinstance(value, dict):
        raise AuthorityError("%s root is not an object" % label)
    return value


def _exact_text_sha(text: Any, digest: Any, label: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise AuthorityError("%s text is missing" % label)
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise AuthorityError("%s SHA-256 is invalid" % label)
    got = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if got != digest:
        raise AuthorityError("%s text SHA-256 mismatch" % label)


def _release_ids(value: Any, start_date: Any, end_date: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise AuthorityError("authorized_input_release_ids must be nonempty")
    if any(not isinstance(item, str) for item in value) or len(value) != len(set(value)):
        raise AuthorityError("authorized input release IDs are invalid or duplicated")
    dates = []
    for release_id in value:
        match = V3_RELEASE_RE.fullmatch(release_id)
        if match is None:
            raise AuthorityError("authorized input is not an exact v3 release ID")
        dates.append(match.group(1))
    if dates != sorted(dates) or dates[0] != start_date or dates[-1] != end_date:
        raise AuthorityError("authorized release IDs do not bind the declared window")
    first = dt.date.fromisoformat(str(start_date))
    final = dt.date.fromisoformat(str(end_date))
    expected = []
    while first <= final:
        expected.append(first.isoformat())
        first += dt.timedelta(days=1)
    if dates != expected:
        raise AuthorityError("authorized release IDs are not one contiguous daily window")
    return list(value)


def validate_authority(
    *,
    authority_path: Path,
    arm_path: Path,
    plan_path: Path,
    runtime_commit_path: Path,
    expected_owner_uid: int = 0,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.timezone.utc)
    authority_raw = _secure_bytes(
        Path(authority_path),
        label="AUTHORITY.json",
        max_bytes=1024 * 1024,
        expected_owner_uid=expected_owner_uid,
    )
    arm_raw = _secure_bytes(
        Path(arm_path),
        label="execution arm",
        max_bytes=128 * 1024,
        expected_owner_uid=expected_owner_uid,
    )
    plan_raw = _secure_bytes(
        Path(plan_path),
        label="adopted plan",
        max_bytes=16 * 1024 * 1024,
        expected_owner_uid=expected_owner_uid,
    )
    runtime_raw = _secure_bytes(
        Path(runtime_commit_path),
        label="runtime commit",
        max_bytes=256,
        expected_owner_uid=expected_owner_uid,
    )
    authority = _json(authority_raw, "AUTHORITY.json")
    arm = _json(arm_raw, "execution arm")
    authority_sha = hashlib.sha256(authority_raw).hexdigest()

    fixed = {
        "schema_version": AUTHORITY_SCHEMA,
        "state": "ACTIVE",
        "authorized_phase_id": PHASE,
        "authorized_work_package_id": WORK_PACKAGE,
        "authorized_instance_id": INSTANCE_ID,
        "authorized_role": ROLE,
        "mode": MODE,
        "execution_class": "EXPLORATORY_ONLY",
        "adopted_plan_path": PLAN_INSTALL_PATH,
        "allowed_network_operations": NETWORK_OPERATIONS,
    }
    for field, expected in fixed.items():
        if authority.get(field) != expected:
            raise AuthorityError("AUTHORITY.json field mismatch: %s" % field)
    release_id = authority.get("release_id")
    if not isinstance(release_id, str) or RELEASE_RE.fullmatch(release_id) is None:
        raise AuthorityError("authority release_id is not an exact D3-W2A release")
    for field in FALSE_AUTHORITY_FIELDS:
        if authority.get(field) is not False:
            raise AuthorityError("authority field must be explicit false: %s" % field)
    _exact_text_sha(
        authority.get("operator_text_verbatim"),
        authority.get("operator_text_sha256"),
        "operator authorization",
    )
    operator_text = authority["operator_text_verbatim"]
    if release_id not in operator_text or WORK_PACKAGE not in operator_text:
        raise AuthorityError(
            "operator text does not name the exact release and work package"
        )
    issued = _utc(authority.get("issued_at_utc"), "authority issued_at_utc")
    expires = _utc(authority.get("expires_at_utc"), "authority expires_at_utc")
    if not issued < expires or now < issued or now >= expires:
        raise AuthorityError("authority is not active in its exact UTC window")
    plan_sha = authority.get("adopted_plan_sha256")
    if not isinstance(plan_sha, str) or not SHA256_RE.fullmatch(plan_sha):
        raise AuthorityError("adopted plan SHA-256 is invalid")
    if hashlib.sha256(plan_raw).hexdigest() != plan_sha:
        raise AuthorityError("adopted plan bytes differ from authority")

    try:
        runtime_commit = runtime_raw.decode("ascii").strip()
    except UnicodeError as exc:
        raise AuthorityError("runtime commit is not ASCII") from exc
    if COMMIT_RE.fullmatch(runtime_commit) is None:
        raise AuthorityError("runtime commit is invalid")
    if authority.get("base_commit") != runtime_commit:
        raise AuthorityError("authority base_commit differs from installed runtime")
    write_roots = authority.get("authorized_write_roots")
    if (
        not isinstance(write_roots, list)
        or len(write_roots) != len(WRITE_ROOTS)
        or set(write_roots) != WRITE_ROOTS
    ):
        raise AuthorityError("authority write roots differ from the fixed W09 roots")
    spending_cap = authority.get("spending_cap_usd")
    if (
        not isinstance(spending_cap, (int, float))
        or isinstance(spending_cap, bool)
        or spending_cap <= 0
    ):
        raise AuthorityError("positive W09 spending_cap_usd was not authorized")
    max_runtime = authority.get("max_runtime_seconds")
    if (
        not isinstance(max_runtime, int)
        or isinstance(max_runtime, bool)
        or max_runtime <= 0
        or max_runtime > 86400
    ):
        raise AuthorityError("max_runtime_seconds must be in [1,86400]")
    start_date = authority.get("input_start_date")
    end_date = authority.get("input_end_date")
    try:
        release_ids = _release_ids(
            authority.get("authorized_input_release_ids"),
            start_date,
            end_date,
        )
    except ValueError as exc:
        raise AuthorityError("authority input dates are invalid") from exc

    arm_fixed = {
        "schema_version": ARM_SCHEMA,
        "state": "ARMED",
        "release_id": release_id,
        "authority_sha256": authority_sha,
        "base_commit": runtime_commit,
        "authorized_work_package_id": WORK_PACKAGE,
        "mode": MODE,
    }
    for field, expected in arm_fixed.items():
        if arm.get(field) != expected:
            raise AuthorityError("execution arm field mismatch: %s" % field)
    armed = _utc(arm.get("armed_at_utc"), "arm armed_at_utc")
    arm_expires = _utc(arm.get("expires_at_utc"), "arm expires_at_utc")
    if armed < issued or armed >= arm_expires or arm_expires > expires:
        raise AuthorityError("arm window is outside the authority window")
    if now < armed or now >= arm_expires:
        raise AuthorityError("execution arm is not active")
    active_window_seconds = int((min(expires, arm_expires) - now).total_seconds())
    if active_window_seconds <= 0:
        raise AuthorityError("authority/arm active window is exhausted")

    return {
        "schema_version": "deep03-w09-authority-gate-pass-v1",
        "state": "AUTHORIZED",
        "release_id": release_id,
        "authority_sha256": authority_sha,
        "arm_sha256": hashlib.sha256(arm_raw).hexdigest(),
        "adopted_plan_sha256": plan_sha,
        "base_commit": runtime_commit,
        "mode": MODE,
        "authorized_phase_id": PHASE,
        "authorized_work_package_id": WORK_PACKAGE,
        "input_start_date": start_date,
        "input_end_date": end_date,
        "authorized_input_release_ids": release_ids,
        "spending_cap_usd": spending_cap,
        "max_runtime_seconds": max_runtime,
        "effective_runtime_seconds": min(max_runtime, active_window_seconds),
        "expires_at_utc": authority["expires_at_utc"],
        "arm_expires_at_utc": arm["expires_at_utc"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authority", required=True, type=Path)
    parser.add_argument("--arm-file", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--runtime-commit", required=True, type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = validate_authority(
            authority_path=args.authority,
            arm_path=args.arm_file,
            plan_path=args.plan,
            runtime_commit_path=args.runtime_commit,
        )
    except AuthorityError as exc:
        print("D3_W2A_AUTHORITY_REFUSED: %s" % exc, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        print(
            "D3_W2A_AUTHORITY_PASS release=%s authority_sha256=%s"
            % (result["release_id"], result["authority_sha256"])
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
