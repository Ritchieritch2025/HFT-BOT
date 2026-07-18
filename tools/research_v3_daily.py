#!/usr/bin/env python3
"""Fail-safe daily orchestrator for manifest-only research v3 releases.

This process is deliberately separate from capture, ingest, export, and the
seal chain.  It consumes an already-published durable canonical receipt,
applies eligibility tags through the dedicated tagger identity, and invokes
the existing v3 reference publisher.  A failure exits non-zero and records a
small local status; it never starts, stops, reloads, or signals a production
service.

RFQ is structurally off.  Queue entries are dates only, durable receipts that
contain an RFQ object are rejected, the tagger is never given an RFQ option,
and the publisher is always invoked with ``--no-rfq``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_TAGGER_ARN = (
    "arn:aws:iam::321572485933:user/canonical-eligibility-tagger"
)
DEFAULT_TAGGER_PROFILE = "canonical-eligibility-tagger"
DEFAULT_PUBLISHER_ARN = "arn:aws:iam::321572485933:user/vaultWriter"
DEFAULT_BUCKET = "kalshi-vault-ritcardo"
DEFAULT_PREFIX = "ec2"
DEFAULT_DEST = "s3://kalshi-vault-ritcardo/research"
DEFAULT_PRODUCTION_LIVE_DIR = pathlib.Path("/home/ubuntu/hft-bot/work/live")
DEFAULT_PRODUCTION_RAW_ROOT = pathlib.Path("/home/ubuntu/hft-bot/work/raw")
DEFAULT_PRODUCTION_WAREHOUSE_ROOT = pathlib.Path(
    "/home/ubuntu/hft-bot/work/warehouse")
DEFAULT_PRODUCTION_QUALITY_DIR = pathlib.Path(
    "/home/ubuntu/hft-bot/work/event_packs")
DEFAULT_PRODUCTION_HOME = pathlib.Path("/var/lib/kalshi-research-v3")
DEFAULT_CREDENTIAL_DIRECTORY = pathlib.Path(
    "/run/credentials/kalshi-research-v3-daily.service")
DEFAULT_PUBLISHER_ENV_FILE = DEFAULT_CREDENTIAL_DIRECTORY / "publisher.env"
DEFAULT_TAGGER_CREDENTIAL_FILE = (
    DEFAULT_CREDENTIAL_DIRECTORY / "tagger.credentials")
DEFAULT_DURABLE_CREDENTIAL_DIRECTORY = pathlib.Path(
    "/run/credentials/kalshi-research-v3-durable.service")
DEFAULT_DURABLE_PUBLISHER_ENV_FILE = (
    DEFAULT_DURABLE_CREDENTIAL_DIRECTORY / "publisher.env")
TAGGER_POLICY_SHA256 = (
    "2fa7dbe4103f01b26fe871c1a0c8f215f2aba4396e6902db1cdcf08eeae275a0")
BUCKET_POLICY_SHA256 = (
    "d9961561ce2e434a40bde5f62e716c0567a1148c0a5f9b42a07a8b70d2bb02d5")
PUBLISHER_DELTA_SHA256 = (
    "f0b8bc170ac676d6d2e3f57b8de696e6af94195fc154513a7bbd293ce8a987bf")
OPERATOR_AUTHORIZATION_SHA256 = (
    "3c77302612e70fdb5e6534657812c581a78ab93cf9dde6ab0e11c84e84940910")
AUTOMATION_EXECUTION_AUTHORIZATION_SHA256 = (
    "1b14001428f2387f3e62c531a8d8ce3dd4f8bd726d6c8b94b4d6c0d893020761")
AUTOMATION_MIN_DATE = "2026-07-10"
DEFAULT_TAGGER_POLICY = (ROOT / "docs" / "plan_releases" / "pipeline"
                         / "W-PUB-REF-01C_TAGGER_IDENTITY_POLICY.json")
DEFAULT_BUCKET_POLICY = (ROOT / "docs" / "plan_releases" / "pipeline"
                         / "W-PUB-REF-01C_BUCKET_POLICY_FULL_TARGET_2026-07-17.json")
DEFAULT_PUBLISHER_DELTA = (ROOT / "docs" / "plan_releases" / "pipeline"
                           / "W-PUB-REF-01C_PUBLISHER_TAG_INSPECTION_DELTA.json")
DEFAULT_OPERATOR_AUTHORIZATION = (
    ROOT / "docs" / "plan_releases" / "pipeline"
    / "W-PUB-REF-01C_OPERATOR_ATTESTED_AUTHORIZATION_2026-07-17.json")
DEFAULT_APPROVAL_DIRECTORY = pathlib.Path(
    "/etc/kalshi-research-v3/approvals")
DEFAULT_CUTOVER_ARM = DEFAULT_APPROVAL_DIRECTORY / "cutover-approved"
DEFAULT_PUBLISH_ARM = DEFAULT_APPROVAL_DIRECTORY / "publish-approved"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
RFQ_BASENAME_RE = re.compile(
    r"^rfq(?:_receipts)?_\d{2}\.ndjson(?:\.\d+)?$"
)
MAX_QUEUE_BYTES = 64 * 1024
MAX_INDEX_BYTES = 1024 * 1024
MAX_RECEIPT_BYTES = 16 * 1024 * 1024
MAX_AUDIT_BYTES = 64 * 1024
MAX_POLICY_EVIDENCE_BYTES = 1024 * 1024
MAX_REFERENCE_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_REFERENCE_PREPARED_PLAN_BYTES = 32 * 1024 * 1024
CANONICAL_VERIFY_WORKERS = 4
TERMINAL_STATUS_SCHEMA = "research-v3-daily-status-v2"
PREPARED_PLAN_SCHEMA = "research-reference-prepared-plan-v1"
PREPARED_PLAN_STATE = "REFERENCE_MANIFEST_PREPARED"
MANIFEST_COMMIT_RESULT_SCHEMA = \
    "research-reference-manifest-commit-result-v1"


class GateError(RuntimeError):
    """One stable, fail-closed refusal."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _date_scope_allowed(date: str) -> bool:
    return _validate_date(date) >= AUTOMATION_MIN_DATE


def _record_date_scope_block(failures: list[dict] | None, date: str,
                             source: str) -> None:
    issue = {
        "date": date,
        "code": "AUTHORIZATION_DATE_SCOPE_BLOCKED",
        "detail": (
            f"{source}: automation authorization begins at "
            f"{AUTOMATION_MIN_DATE}; older dates are not authorized"),
    }
    if failures is None:
        raise GateError(issue["code"], issue["detail"])
    if not any(row.get("date") == date and row.get("code") == issue["code"]
               for row in failures):
        failures.append(issue)


@dataclass(frozen=True)
class DatePlan:
    date: str
    durable_index: pathlib.Path
    durable_receipt: pathlib.Path
    durable_set_sha256: str
    audit: pathlib.Path
    policy_evidence: pathlib.Path
    tagged_index: pathlib.Path | None
    audit_sha256: str
    pending_index: pathlib.Path | None = None
    historical_existing_only: bool = False


def _validate_date(value: str) -> str:
    if not isinstance(value, str) or DATE_RE.fullmatch(value) is None:
        raise GateError("DATE_INVALID", repr(value))
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise GateError("DATE_INVALID", str(exc)) from exc
    if parsed.isoformat() != value:
        raise GateError("DATE_INVALID", value)
    return value


def _fingerprint(item: os.stat_result) -> tuple[int, int, int, int, int]:
    return (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns,
            item.st_ctime_ns)


def _read_regular(path: pathlib.Path, limit: int, label: str) -> bytes:
    """Read one bounded, stable regular file without following a symlink."""
    path = pathlib.Path(path).absolute()
    if not hasattr(os, "O_NOFOLLOW"):
        raise GateError("LOCAL_SAFETY_UNAVAILABLE", "O_NOFOLLOW is required")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW |
                     getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise GateError("LOCAL_INPUT_INVALID", f"{label}: {exc}") from exc
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_size <= 0
                or before.st_size > limit):
            raise GateError(
                "LOCAL_INPUT_INVALID",
                f"{label} must be a non-empty regular file <= {limit} bytes",
            )
        chunks: list[bytes] = []
        total = 0
        while total <= limit:
            chunk = os.read(fd, min(1 << 20, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(fd)
        try:
            named = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise GateError("LOCAL_INPUT_CHANGED", f"{label}: {exc}") from exc
        if (total > limit or total != before.st_size
                or _fingerprint(before) != _fingerprint(after)
                or stat.S_ISLNK(named.st_mode)
                or (named.st_dev, named.st_ino) != (after.st_dev, after.st_ino)):
            raise GateError("LOCAL_INPUT_CHANGED", label)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _json_no_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise GateError("JSON_INVALID", f"duplicate key {key!r}")
        value[key] = item
    return value


def _read_json(path: pathlib.Path, limit: int, label: str) -> tuple[dict, bytes]:
    raw = _read_regular(path, limit, label)
    try:
        value = json.loads(raw, object_pairs_hook=_json_no_duplicates)
    except GateError:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        raise GateError("JSON_INVALID", f"{label}: {exc}") from exc
    if not isinstance(value, dict):
        raise GateError("JSON_INVALID", f"{label} root is not an object")
    return value, raw


def _secure_secret_file(path: pathlib.Path, label: str) -> pathlib.Path:
    """Validate a credential source without reading or printing its content."""
    path = pathlib.Path(path).absolute()
    try:
        item = os.stat(path, follow_symlinks=False)
        parent = os.stat(path.parent, follow_symlinks=False)
    except OSError as exc:
        raise GateError("CREDENTIAL_FILE_INVALID", f"{label}: {exc}") from exc
    file_mode = stat.S_IMODE(item.st_mode)
    parent_mode = stat.S_IMODE(parent.st_mode)
    # LoadCredentialEncrypted on production systemd 255 yields a root:root
    # 0440 file in a root:root 0550 private mount.  Local fixtures use the
    # equally strict caller-owned 0600/0700 form.  Reject every broader mode.
    systemd_shape = (
        item.st_uid == item.st_gid == 0 and file_mode in {0o400, 0o440}
        and parent.st_uid == parent.st_gid == 0
        and parent_mode in {0o500, 0o550})
    private_shape = (
        item.st_uid in {0, os.geteuid()} and file_mode == 0o600
        and parent.st_uid in {0, os.geteuid()} and parent_mode == 0o700)
    if (not stat.S_ISREG(item.st_mode) or stat.S_ISLNK(item.st_mode)
            or not (systemd_shape or private_shape)):
        raise GateError(
            "CREDENTIAL_FILE_INVALID",
            f"{label} must be an exact systemd 0440 or private 0600 credential",
        )
    if not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode):
        raise GateError(
            "CREDENTIAL_FILE_INVALID",
            f"{label} parent must be exact systemd 0550 or private 0700",
        )
    if item.st_size <= 0 or item.st_size > 64 * 1024:
        raise GateError("CREDENTIAL_FILE_INVALID", f"{label} size is invalid")
    return path


def _require_arm(path: pathlib.Path, label: str) -> None:
    """Require an operator-owned approval file without following a symlink."""
    path = pathlib.Path(path)
    try:
        raw = _read_regular(path, 4096, label)
    except GateError as exc:
        raise GateError("OPERATOR_GATE", f"{label}: {exc.detail}") from exc
    try:
        item = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise GateError("OPERATOR_GATE", f"{label}: {exc}") from exc
    if (not stat.S_ISREG(item.st_mode) or stat.S_ISLNK(item.st_mode)
            or item.st_uid not in {0, os.geteuid()} or item.st_mode & 0o022
            or item.st_size > 4096):
        raise GateError(
            "OPERATOR_GATE",
            f"{label} must be a caller-owned, non-symlink, non-writable approval file",
        )
    if hashlib.sha256(raw).hexdigest() != \
            AUTOMATION_EXECUTION_AUTHORIZATION_SHA256:
        raise GateError(
            "OPERATOR_GATE",
            f"{label} does not match the authorized automation execution scope",
        )


def _require_arms(args) -> None:
    _require_arm(pathlib.Path(args.cutover_arm_file), "v3 cutover arm")
    _require_arm(pathlib.Path(args.publish_arm_file), "research publish arm")


def _exact_existing_dir(path: pathlib.Path, expected: pathlib.Path,
                        label: str) -> pathlib.Path:
    """Pin production data roots; resolving either side also rejects symlinks."""
    path = pathlib.Path(path)
    if not path.is_absolute():
        raise GateError("PRODUCTION_PATH_INVALID", f"{label} must be absolute")
    try:
        actual = path.resolve(strict=True)
        wanted = pathlib.Path(expected).resolve(strict=True)
    except OSError as exc:
        raise GateError("PRODUCTION_PATH_INVALID", f"{label}: {exc}") from exc
    if actual != wanted or not actual.is_dir():
        raise GateError(
            "PRODUCTION_PATH_INVALID",
            f"{label} resolved to {actual}, expected {wanted}",
        )
    return actual


_PUBLISHER_AWS_KEYS = {
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_DEFAULT_REGION",
    "AWS_REGION",
}


def publisher_env(home: pathlib.Path, env_file: pathlib.Path,
                  region: str) -> dict[str, str]:
    """Parse simple assignments and export only an explicit AWS allowlist.

    The existing production ``env.sh`` may also contain trading credentials.
    It is intentionally never sourced: non-AWS assignments are ignored and
    commands/expansions are rejected, so the publisher subprocess receives no
    trading secret or caller-controlled Python/root override.
    """
    raw = _read_regular(env_file, 64 * 1024, "publisher environment file")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GateError("CREDENTIAL_FILE_INVALID", "publisher env is not UTF-8") from exc
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        try:
            tokens = shlex.split(line, comments=True, posix=True)
        except ValueError as exc:
            raise GateError(
                "CREDENTIAL_FILE_INVALID",
                f"publisher env line {number}: {exc}",
            ) from exc
        if not tokens:
            continue
        if tokens[0] == "export":
            tokens = tokens[1:]
        if len(tokens) != 1 or "=" not in tokens[0]:
            raise GateError(
                "CREDENTIAL_FILE_INVALID",
                f"publisher env line {number} is not one assignment",
            )
        key, value = tokens[0].split("=", 1)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
            raise GateError(
                "CREDENTIAL_FILE_INVALID",
                f"publisher env line {number} has an invalid key",
            )
        if key.startswith("AWS_") and key not in _PUBLISHER_AWS_KEYS:
            raise GateError(
                "CREDENTIAL_FILE_INVALID",
                f"publisher env line {number} sets forbidden {key}",
            )
        if key in _PUBLISHER_AWS_KEYS:
            if any(marker in value for marker in ("$", "`", "\x00")):
                raise GateError(
                    "CREDENTIAL_FILE_INVALID",
                    f"publisher env line {number} contains expansion syntax",
                )
            values[key] = value
    if not values.get("AWS_ACCESS_KEY_ID") or not values.get(
            "AWS_SECRET_ACCESS_KEY"):
        raise GateError(
            "CREDENTIAL_FILE_INVALID", "publisher AWS keys are missing")
    values.setdefault("AWS_DEFAULT_REGION", region)
    values.setdefault("AWS_REGION", region)
    env = _base_env(home)
    env.update(values)
    return env


def _is_rfq_object(obj: object) -> bool:
    if not isinstance(obj, dict):
        return False
    key = str(obj.get("key") or obj.get("source_key") or "")
    logical = str(obj.get("logical_source_key") or "")
    source_kind = str(obj.get("source_kind") or "")
    basenames = {os.path.basename(value) for value in (key, logical) if value}
    return (obj.get("channel") == "rfq"
            or source_kind.startswith("raw_rfq")
            or any(RFQ_BASENAME_RE.fullmatch(name) is not None
                   for name in basenames))


def _require_rfq_off(receipt: dict, label: str) -> None:
    for flag in ("rfq_included", "include_rfq", "include_sealed_rfq"):
        if receipt.get(flag) not in (None, False):
            raise GateError("RFQ_GATE", f"{label} sets {flag}")
    objects = receipt.get("objects")
    if not isinstance(objects, list) or not objects:
        raise GateError("DURABLE_RECEIPT_INVALID", f"{label} objects are empty")
    for obj in objects:
        if not isinstance(obj, dict):
            raise GateError("DURABLE_RECEIPT_INVALID", f"{label} object malformed")
        if _is_rfq_object(obj):
            raise GateError(
                "RFQ_GATE",
                f"{label} contains RFQ object {obj.get('key')!r}",
            )
        key = str(obj.get("key") or "")
        if obj.get("research_candidate") is True and "/raw/" in f"/{key}":
            raise GateError("RFQ_GATE", f"{label} exposes a raw candidate")
    for family in receipt.get("families") or []:
        if not isinstance(family, dict):
            raise GateError("DURABLE_RECEIPT_INVALID", "family malformed")
        if "rfq" not in str(family.get("name") or "").lower():
            continue
        allowed = (family.get("state") == "NOT_APPLICABLE"
                   and family.get("observed_count") == 0
                   and family.get("reason_code")
                   == "RFQ_BRANCH_CLOSED_NO_REPAIR")
        if not allowed:
            raise GateError("RFQ_GATE", "RFQ family is not hard-closed")


def _validate_durable_index(path: pathlib.Path, date: str) -> tuple[dict, pathlib.Path]:
    index, _raw = _read_json(path, MAX_INDEX_BYTES, "durable index")
    digest = index.get("receipt_set_sha256")
    binding = index.get("receipt_object")
    if (index.get("schema_version") != "canonical-durable-receipt-index-v1"
            or index.get("state") != "DURABLE_RECEIPT_VERIFIED"
            or index.get("date") != date
            or index.get("complete") is not True
            or index.get("prune_eligible") is not False
            or not isinstance(digest, str) or SHA_RE.fullmatch(digest) is None
            or not isinstance(binding, dict)
            or binding.get("bucket") != DEFAULT_BUCKET
            or not isinstance(binding.get("VersionId"), str)
            or not binding["VersionId"]
            or binding.get("verification_state") != "EXACT_VERSION_FULL_SHA256"):
        raise GateError("DURABLE_NOT_READY", str(path))
    if path.name != f"DURABLE-{digest}.json":
        raise GateError("DURABLE_NOT_READY", "index filename/digest mismatch")
    expected_key = (
        f"{DEFAULT_PREFIX}/control/canonical-receipts/v1/date={date}/"
        f"receipt-{digest}.json"
    )
    if binding.get("key") != expected_key:
        raise GateError("DURABLE_NOT_READY", "receipt key/digest mismatch")
    receipt_path = path.with_name(f"receipt-{digest}.json")
    receipt, raw = _read_json(receipt_path, MAX_RECEIPT_BYTES, "durable receipt")
    seal = receipt.get("seal")
    if (receipt.get("receipt_set_sha256") != digest
            or receipt.get("date") != date
            or receipt.get("state") != "DURABLE_RECEIPT_VERIFIED"
            or receipt.get("authority") != "CANONICAL_CONTROL_PLANE"
            or receipt.get("authoritative") is not True
            or receipt.get("s3_published") is not True
            or not isinstance(seal, dict)
            or seal.get("date") != date
            or seal.get("status") != "SEALED"
            or seal.get("version") != 2
            or binding.get("size") != len(raw)
            or binding.get("sha256") != hashlib.sha256(raw).hexdigest()):
        raise GateError("DURABLE_NOT_READY", "receipt/index binding mismatch")
    _require_rfq_off(receipt, "durable receipt")
    return index, receipt_path


def _parse_utc(value: object, label: str) -> dt.datetime:
    if not isinstance(value, str):
        raise GateError("SINGLE_WRITER_AUDIT_INVALID", f"{label} is missing")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GateError("SINGLE_WRITER_AUDIT_INVALID", f"{label}: {exc}") from exc
    if parsed.tzinfo is None:
        raise GateError("SINGLE_WRITER_AUDIT_INVALID", f"{label} is timezone-naive")
    return parsed.astimezone(dt.timezone.utc)


def _validate_audit(audit_path: pathlib.Path, evidence_path: pathlib.Path,
                    date: str, receipt_sha: str, tagger_arn: str, *,
                    require_fresh: bool = True,
                    expected_sha256: str | None = None,
                    now: dt.datetime | None = None) -> str:
    audit, audit_raw = _read_json(audit_path, MAX_AUDIT_BYTES,
                                  "single-writer audit")
    evidence = _read_regular(evidence_path, MAX_POLICY_EVIDENCE_BYTES,
                             "single-writer policy evidence")
    audit_sha = hashlib.sha256(audit_raw).hexdigest()
    if expected_sha256 is not None and audit_sha != expected_sha256:
        raise GateError(
            "SINGLE_WRITER_AUDIT_INVALID", "historical audit digest mismatch")
    audited = _parse_utc(audit.get("audited_at_utc"), "audited_at_utc")
    current = (now or dt.datetime.now(dt.timezone.utc)).astimezone(
        dt.timezone.utc)
    if require_fresh and (audited > current + dt.timedelta(minutes=5)
                          or current - audited > dt.timedelta(hours=24)):
        raise GateError(
            "SINGLE_WRITER_AUDIT_STALE", audited.isoformat())
    if (audit.get("schema_version")
            != "canonical-eligibility-single-writer-audit-v1"
            or audit.get("state") != "SINGLE_WRITER_VERIFIED"
            or audit.get("date") != date
            or audit.get("bucket") != DEFAULT_BUCKET
            or audit.get("receipt_set_sha256") != receipt_sha
            or audit.get("tag_key") != "research-eligible"
            or audit.get("dedicated_tagger_principal") != tagger_arn
            or audit.get("dedicated_tagger_sts_caller_arn", tagger_arn)
            != tagger_arn
            or audit.get("exclusive_exact_version_writer") is not True
            or audit.get("versionless_tagging_denied") is not True
            or audit.get("other_automation_tag_writers_denied") is not True
            or not isinstance(audit.get("auditor"), str)
            or not audit["auditor"].strip()
            or audit.get("policy_evidence_size") != len(evidence)
            or audit.get("policy_evidence_sha256")
            != hashlib.sha256(evidence).hexdigest()):
        raise GateError("SINGLE_WRITER_AUDIT_INVALID", str(audit_path))
    return audit_sha


def _validate_tagged_index(path: pathlib.Path, date: str,
                           byte_receipt_sha: str) -> str:
    index, _raw = _read_json(path, MAX_INDEX_BYTES, "tagged durable index")
    digest = index.get("receipt_set_sha256")
    audit_sha = index.get("eligibility_single_writer_audit_sha256")
    binding = index.get("receipt_object")
    if (index.get("schema_version") != "canonical-durable-receipt-index-v1"
            or index.get("state") != "DURABLE_RECEIPT_VERIFIED"
            or index.get("date") != date
            or index.get("complete") is not True
            or index.get("completed") is not True
            or index.get("prune_eligible") is not False
            or index.get("receipt_phase") != "TAGGED_ELIGIBILITY_VERIFIED"
            or index.get("byte_attestation_receipt_set_sha256")
            != byte_receipt_sha
            or index.get("receipt_object_eligibility_tag_state")
            != "TAGGED_VERIFIED"
            or not isinstance(digest, str) or SHA_RE.fullmatch(digest) is None
            or not isinstance(audit_sha, str)
            or SHA_RE.fullmatch(audit_sha) is None
            or not isinstance(binding, dict)
            or binding.get("bucket") != DEFAULT_BUCKET
            or not isinstance(binding.get("key"), str)
            or binding.get("key") != (
                f"{DEFAULT_PREFIX}/control/canonical-receipts/v1/date={date}/"
                f"receipt-{digest}.json")
            or not isinstance(binding.get("VersionId"), str)
            or not binding["VersionId"]
            or binding.get("verification_state")
            != "EXACT_VERSION_FULL_SHA256"
            or path.name != f"TAGGED-DURABLE-{digest}.json"):
        raise GateError("TAGGED_INDEX_INVALID", str(path))
    receipt_path = path.with_name(f"receipt-{digest}.json")
    receipt, receipt_raw = _read_json(
        receipt_path, MAX_RECEIPT_BYTES, "tagged durable receipt")
    if (receipt.get("schema_version") != "canonical-object-receipt-v1"
            or receipt.get("state") != "DURABLE_RECEIPT_VERIFIED"
            or receipt.get("authority") != "CANONICAL_CONTROL_PLANE"
            or receipt.get("authoritative") is not True
            or receipt.get("s3_published") is not True
            or receipt.get("date") != date
            or receipt.get("prune_eligible") is not False
            or receipt.get("receipt_set_sha256") != digest
            or receipt.get("byte_attestation_receipt_set_sha256")
            != byte_receipt_sha
            or receipt.get("receipt_phase") != "TAGGED_ELIGIBILITY_VERIFIED"
            or receipt.get("eligibility_single_writer_audit_sha256")
            != audit_sha
            or binding.get("size") != len(receipt_raw)
            or binding.get("sha256") != hashlib.sha256(receipt_raw).hexdigest()
            or index.get("receipt_payload_size") != len(receipt_raw)
            or index.get("receipt_payload_sha256")
            != hashlib.sha256(receipt_raw).hexdigest()):
        raise GateError("TAGGED_INDEX_INVALID", "tagged receipt binding mismatch")
    _require_rfq_off(receipt, "tagged durable receipt")
    return audit_sha


def _one_match(root: pathlib.Path, pattern: str, label: str) -> pathlib.Path | None:
    paths = sorted(path for path in root.glob(pattern) if path.is_file())
    if len(paths) > 1:
        raise GateError("AMBIGUOUS_INPUT", f"multiple {label} files under {root}")
    return paths[0] if paths else None


def _find_durable(date: str, live_dir: pathlib.Path,
                  receipt_root: pathlib.Path | None) -> pathlib.Path | None:
    roots = [pathlib.Path(live_dir) / "canonical_receipts"]
    if receipt_root is not None:
        fallback = pathlib.Path(receipt_root).absolute()
        if fallback not in roots:
            roots.append(fallback)
    by_digest: dict[str, pathlib.Path] = {}
    for root in roots:
        durable_dir = root / "durable" / f"date={date}"
        for path in sorted(durable_dir.glob("DURABLE-*.json")):
            match = re.fullmatch(r"DURABLE-([0-9a-f]{64})\.json", path.name)
            if path.is_file() and match and match.group(1) not in by_digest:
                by_digest[match.group(1)] = path
    if len(by_digest) > 1:
        raise GateError(
            "AMBIGUOUS_DURABLE_AUTHORITY",
            f"{date} has multiple content-addressed durable receipts; "
            "an explicit canonical authority pointer is required",
        )
    return next(iter(by_digest.values()), None)


def _audit_pair(audit_root: pathlib.Path, date: str,
                receipt_sha: str) -> tuple[pathlib.Path, pathlib.Path]:
    root = (pathlib.Path(audit_root).absolute() / f"date={date}"
            / f"receipt={receipt_sha}")
    pointer = root / "CURRENT.json"
    if pointer.exists():
        value, _raw = _read_json(pointer, MAX_AUDIT_BYTES,
                                 "current audit pointer")
        audit_name = value.get("audit_file")
        evidence_name = value.get("policy_evidence_file")
        audit_sha = value.get("audit_sha256")
        evidence_sha = value.get("policy_evidence_sha256")
        if (value.get("schema_version")
                != "research-v3-current-policy-audit-pointer-v1"
                or value.get("date") != date
                or value.get("receipt_set_sha256") != receipt_sha
                or not isinstance(audit_name, str)
                or pathlib.Path(audit_name).name != audit_name
                or audit_name != f"single-writer-audit-{audit_sha}.json"
                or not isinstance(audit_sha, str)
                or SHA_RE.fullmatch(audit_sha) is None
                or not isinstance(evidence_name, str)
                or pathlib.Path(evidence_name).name != evidence_name
                or evidence_name != f"policy-evidence-{evidence_sha}.json"
                or not isinstance(evidence_sha, str)
                or SHA_RE.fullmatch(evidence_sha) is None):
            raise GateError("SINGLE_WRITER_AUDIT_INVALID", str(pointer))
        audit_path = root / audit_name
        evidence_path = root / evidence_name
        if (hashlib.sha256(_read_regular(
                audit_path, MAX_AUDIT_BYTES, "current audit")).hexdigest()
                != audit_sha
                or hashlib.sha256(_read_regular(
                    evidence_path, MAX_POLICY_EVIDENCE_BYTES,
                    "current policy evidence")).hexdigest() != evidence_sha):
            raise GateError("SINGLE_WRITER_AUDIT_INVALID", str(pointer))
        return audit_path, evidence_path
    # Compatibility for the first operator-created canary.  New automation
    # writes only the digest-scoped layout above and never overwrites history.
    legacy = pathlib.Path(audit_root).absolute() / f"date={date}"
    return legacy / "single-writer-audit.json", legacy / "policy-evidence.json"


def _historical_audit_pair(audit_root: pathlib.Path, date: str,
                           receipt_sha: str,
                           audit_sha: str) -> tuple[pathlib.Path, pathlib.Path]:
    root = (pathlib.Path(audit_root).absolute() / f"date={date}"
            / f"receipt={receipt_sha}")
    audit = root / f"single-writer-audit-{audit_sha}.json"
    if audit.exists():
        payload, _raw = _read_json(audit, MAX_AUDIT_BYTES,
                                   "historical single-writer audit")
        evidence_sha = payload.get("policy_evidence_sha256")
        if not isinstance(evidence_sha, str) or SHA_RE.fullmatch(
                evidence_sha) is None:
            raise GateError("SINGLE_WRITER_AUDIT_INVALID", str(audit))
        return audit, root / f"policy-evidence-{evidence_sha}.json"
    legacy = pathlib.Path(audit_root).absolute() / f"date={date}"
    return legacy / "single-writer-audit.json", legacy / "policy-evidence.json"


def prepare_date_plan(date: str, live_dir: pathlib.Path,
                      audit_root: pathlib.Path, tagger_arn: str,
                      receipt_root: pathlib.Path | None = None) -> DatePlan:
    date = _validate_date(date)
    live_dir = pathlib.Path(live_dir).absolute()
    durable = _find_durable(date, live_dir, receipt_root)
    if durable is None:
        raise GateError("DURABLE_NOT_READY", f"no durable index for {date}")
    index, receipt_path = _validate_durable_index(durable, date)
    digest = index["receipt_set_sha256"]
    audit, evidence = _audit_pair(audit_root, date, digest)
    audit_sha = _validate_audit(
        audit, evidence, date, digest, tagger_arn, require_fresh=True)
    tagged_dir = live_dir / "canonical_receipts" / "tagged" / f"date={date}"
    tagged = _one_match(tagged_dir, "TAGGED-DURABLE-*.json",
                        "tagged durable index")
    if tagged is not None:
        historical_sha = _validate_tagged_index(tagged, date, digest)
        historical, historical_evidence = _historical_audit_pair(
            audit_root, date, digest, historical_sha)
        _validate_audit(
            historical, historical_evidence, date, digest, tagger_arn,
            require_fresh=False, expected_sha256=historical_sha)
    return DatePlan(
        date, durable, receipt_path, digest, audit, evidence, tagged,
        audit_sha)


def pending_recovery_plan(date: str, durable: pathlib.Path,
                          live_dir: pathlib.Path, audit_root: pathlib.Path,
                          tagger_arn: str) -> DatePlan | None:
    """Resume the exact historical audit bound by one durable pending intent."""
    index, receipt_path = _validate_durable_index(durable, date)
    parent_sha = index["receipt_set_sha256"]
    root = (pathlib.Path(live_dir).absolute() / "canonical_receipts"
            / "tagged" / f"date={date}")
    pending = sorted(root.glob("TAGGED-PENDING-*.json"))
    if not pending:
        return None
    if len(pending) != 1 or any(root.glob("TAGGED-DURABLE-*.json")):
        raise GateError("TAGGER_RECOVERY_AMBIGUOUS", str(root))
    intent, _raw = _read_json(
        pending[0], MAX_INDEX_BYTES, "tagged pending intent")
    digest = intent.get("receipt_set_sha256")
    audit_sha = intent.get("eligibility_single_writer_audit_sha256")
    if (intent.get("schema_version")
            != "canonical-tagged-receipt-pending-index-v1"
            or intent.get("state")
            != "TAGGED_RECEIPT_OBJECT_TAG_PENDING"
            or intent.get("date") != date
            or intent.get("byte_attestation_receipt_set_sha256") != parent_sha
            or not isinstance(digest, str)
            or pending[0].name != f"TAGGED-PENDING-{digest}.json"
            or not isinstance(audit_sha, str)
            or SHA_RE.fullmatch(audit_sha) is None):
        raise GateError("TAGGER_RECOVERY_INVALID", str(pending[0]))
    audit, evidence = _historical_audit_pair(
        audit_root, date, parent_sha, audit_sha)
    _validate_audit(
        audit, evidence, date, parent_sha, tagger_arn,
        require_fresh=False, expected_sha256=audit_sha)
    created_at = intent.get("transaction_created_at_utc")
    if created_at is not None:
        created = _parse_utc(created_at, "pending transaction_created_at_utc")
    else:
        try:
            pending_stat = os.stat(pending[0], follow_symlinks=False)
        except OSError as exc:
            raise GateError("TAGGER_RECOVERY_INVALID", str(exc)) from exc
        if (not stat.S_ISREG(pending_stat.st_mode)
                or stat.S_ISLNK(pending_stat.st_mode)):
            raise GateError("TAGGER_RECOVERY_INVALID", str(pending[0]))
        created = dt.datetime.fromtimestamp(
            pending_stat.st_mtime, tz=dt.timezone.utc)
    age = dt.datetime.now(dt.timezone.utc) - created
    if age < -dt.timedelta(minutes=5):
        raise GateError("TAGGER_RECOVERY_INVALID", "pending marker is future-dated")
    historical_existing_only = age > dt.timedelta(hours=24)
    return DatePlan(
        date, durable, receipt_path, parent_sha, audit, evidence, None,
        audit_sha, pending[0], historical_existing_only)


def _validate_completed_state(date: str, receipt_sha: str,
                              live_dir: pathlib.Path,
                              audit_root: pathlib.Path,
                              tagger_arn: str) -> pathlib.Path:
    tagged_dir = (pathlib.Path(live_dir) / "canonical_receipts" / "tagged"
                  / f"date={date}")
    tagged = _one_match(
        tagged_dir, "TAGGED-DURABLE-*.json", "tagged durable index")
    if tagged is None:
        raise GateError("COMPLETED_STATE_INVALID", "tagged index is missing")
    historical_sha = _validate_tagged_index(tagged, date, receipt_sha)
    audit, evidence = _historical_audit_pair(
        audit_root, date, receipt_sha, historical_sha)
    _validate_audit(
        audit, evidence, date, receipt_sha, tagger_arn,
        require_fresh=False, expected_sha256=historical_sha)
    tagged_index, _raw = _read_json(
        tagged, MAX_INDEX_BYTES, "completed tagged index")
    stale_pending = tagged.with_name(
        f"TAGGED-PENDING-{tagged_index['receipt_set_sha256']}.json")
    if stale_pending.exists():
        try:
            item = os.stat(stale_pending, follow_symlinks=False)
            if not stat.S_ISREG(item.st_mode) or stat.S_ISLNK(item.st_mode):
                raise GateError(
                    "TAGGER_RECOVERY_INVALID", str(stale_pending))
            os.unlink(stale_pending)
        except OSError as exc:
            raise GateError(
                "TAGGER_RECOVERY_INVALID", str(exc)) from exc
    return tagged


def load_dates(dates: list[str], queue_path: pathlib.Path | None,
               yesterday: bool, now: dt.datetime | None = None, *,
               scan_ready_days: int = 0,
               warehouse_root: pathlib.Path | None = None,
               discovery_failures: list[dict] | None = None) -> list[str]:
    rows = list(dates)
    if queue_path is not None:
        raw = _read_regular(pathlib.Path(queue_path), MAX_QUEUE_BYTES,
                            "historical date queue")
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise GateError("QUEUE_INVALID", "queue must be ASCII") from exc
        for number, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if any(char.isspace() for char in line) or line.startswith("-"):
                raise GateError("QUEUE_INVALID", f"line {number} is not one date")
            rows.append(line)
    if yesterday:
        current = now or dt.datetime.now(dt.timezone.utc)
        if current.tzinfo is None:
            raise GateError("DATE_INVALID", "current time is timezone-naive")
        rows.append((current.astimezone(dt.timezone.utc).date()
                     - dt.timedelta(days=1)).isoformat())
    if scan_ready_days:
        if scan_ready_days < 1 or scan_ready_days > 366:
            raise GateError("QUEUE_INVALID", "--scan-ready-days must be 1..366")
        if warehouse_root is None:
            raise GateError("QUEUE_INVALID", "scan requires --warehouse-root")
        current = now or dt.datetime.now(dt.timezone.utc)
        today = current.astimezone(dt.timezone.utc).date()
        first = today - dt.timedelta(days=scan_ready_days)
        seals = pathlib.Path(warehouse_root).absolute() / "seals"
        for path in sorted(seals.glob("date=*.json")):
            match = re.fullmatch(r"date=(\d{4}-\d{2}-\d{2})\.json", path.name)
            if match is None:
                continue
            try:
                candidate = dt.date.fromisoformat(match.group(1))
            except ValueError as exc:
                if discovery_failures is not None:
                    discovery_failures.append({
                        "date": match.group(1),
                        "code": "DISCOVERED_SEAL_DATE_INVALID",
                        "detail": f"{path.name}: {exc}",
                    })
                continue
            if candidate < first or candidate >= today:
                continue
            try:
                seal, _raw = _read_json(path, MAX_INDEX_BYTES, "day seal")
            except GateError as exc:
                if discovery_failures is not None:
                    discovery_failures.append({
                        "date": match.group(1),
                        "code": "DISCOVERED_SEAL_INVALID",
                        "detail": f"{path.name}: {exc}",
                    })
                continue
            if (seal.get("status") == "SEALED" and seal.get("version") == 2
                    and seal.get("date", match.group(1)) == match.group(1)):
                rows.append(match.group(1))
            elif discovery_failures is not None:
                discovery_failures.append({
                    "date": match.group(1),
                    "code": "DISCOVERED_SEAL_NOT_READY",
                    "detail": f"{path.name} is not a date-bound SEALED v2 seal",
                })
    if not rows:
        if scan_ready_days:
            return []
        raise GateError(
            "QUEUE_EMPTY",
            "provide --date, --queue, --yesterday, or --scan-ready-days",
        )
    result = []
    seen = set()
    for row in rows:
        row = _validate_date(row)
        if not _date_scope_allowed(row):
            _record_date_scope_block(
                discovery_failures, row, "date/queue/scan discovery")
            continue
        if row not in seen:
            result.append(row)
            seen.add(row)
    return result


def _validate_manifest_object_binding(binding: object, release_id: str,
                                      date: str) -> dict:
    if (not isinstance(binding, dict) or set(binding) != {
            "bucket", "key", "VersionId", "size", "sha256",
            "verification_state"}):
        raise GateError(
            "TERMINAL_STATE_INVALID", "MANIFEST exact binding is malformed")
    expected_key = f"research/releases/{release_id}/MANIFEST.json"
    if (binding.get("bucket") != DEFAULT_BUCKET
            or binding.get("key") != expected_key
            or not _valid_version_id(binding.get("VersionId"))
            or not isinstance(binding.get("size"), int)
            or isinstance(binding.get("size"), bool)
            or binding["size"] < 1
            or binding["size"] > MAX_REFERENCE_MANIFEST_BYTES
            or not isinstance(binding.get("sha256"), str)
            or SHA_RE.fullmatch(binding["sha256"]) is None
            or binding.get("verification_state")
            != "EXACT_VERSION_FULL_SHA256"):
        raise GateError(
            "TERMINAL_STATE_INVALID",
            f"MANIFEST binding is not the exact production object for {date}",
        )
    return dict(binding)


def _validate_local_terminal_status(status: object, path: pathlib.Path,
                                    date: str,
                                    live_dir: pathlib.Path):
    """Validate the local half of a terminal receipt.

    This deliberately does not call AWS.  It can prove only that the status
    is a well-formed retry instruction; authenticated exact-version bytes are
    checked later, after the isolated publisher credentials are loaded.
    """
    live = pathlib.Path(live_dir).absolute()
    required = {
        "schema_version", "state", "date",
        "durable_receipt_set_sha256", "tagged_index", "destination",
        "live_dir", "release_id", "prepared_plan_sha256",
        "manifest_commit_state", "manifest_object", "rfq",
        "completed_at_utc",
    }
    receipt_match = re.fullmatch(
        r"receipt=([0-9a-f]{64})", pathlib.Path(path).parent.name)
    receipt_sha = receipt_match.group(1) if receipt_match else None
    release_id = status.get("release_id") if isinstance(status, dict) else None
    release_pattern = (
        re.escape(date) + r"__v3ref__seal-[0-9a-f]{8}__pub-[0-9a-f]{16}")
    if (not isinstance(status, dict) or set(status) != required
            or status.get("schema_version") != TERMINAL_STATUS_SCHEMA
            or status.get("state") != "V3_REFERENCE_PUBLISHED"
            or status.get("date") != date
            or status.get("durable_receipt_set_sha256") != receipt_sha
            or status.get("destination") != DEFAULT_DEST
            or status.get("live_dir") != str(live)
            or status.get("rfq") != "OFF"
            or not isinstance(release_id, str)
            or re.fullmatch(release_pattern, release_id) is None
            or not isinstance(status.get("prepared_plan_sha256"), str)
            or SHA_RE.fullmatch(status["prepared_plan_sha256"]) is None
            or status.get("manifest_commit_state") not in {
                "REFERENCE_MANIFEST_COMMITTED",
                "REFERENCE_MANIFEST_ALREADY_COMMITTED"}):
        raise GateError(
            "TERMINAL_STATE_INVALID", f"{path}: fixed status fields differ")
    binding = _validate_manifest_object_binding(
        status.get("manifest_object"), release_id, date)
    try:
        completed = dt.datetime.fromisoformat(
            str(status["completed_at_utc"]).replace("Z", "+00:00"))
        if completed.tzinfo is None:
            raise ValueError("completion timestamp is timezone-naive")
        durable = _find_durable(date, live, None)
        if durable is None:
            raise GateError(
                "TERMINAL_STATE_INVALID", "durable index is missing")
        durable_index, _receipt = _validate_durable_index(durable, date)
        if durable_index["receipt_set_sha256"] != receipt_sha:
            raise GateError(
                "TERMINAL_STATE_INVALID", "durable receipt binding changed")
        tagged = pathlib.Path(status["tagged_index"])
        expected_root = (live / "canonical_receipts" / "tagged"
                         / f"date={date}").resolve(strict=True)
        resolved = tagged.resolve(strict=True)
        if (not tagged.is_absolute() or tagged.absolute() != resolved
                or resolved.parent != expected_root):
            raise GateError(
                "TERMINAL_STATE_INVALID", "tagged index path is not canonical")
        _validate_tagged_index(resolved, date, receipt_sha)
    except GateError:
        raise
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise GateError("TERMINAL_STATE_INVALID", f"{path}: {exc}") from exc
    return resolved, binding


def discover_unfinished_dates(live_dir: pathlib.Path,
                              failures: list[dict], *,
                              limit: int = 4096) -> list[str]:
    """Recover bounded nonterminal state independently of seal lookback."""
    live = pathlib.Path(live_dir).absolute()
    candidates: set[str] = set()
    state_dates = sorted((live / "research_v3_daily").glob("date=*"))
    tagged_dates = sorted(
        (live / "canonical_receipts" / "tagged").glob("date=*"))
    transaction_dates = []
    for name in ("durable", "controls", "forward-aux",
                 "forward-metadata-preflight", "forward-version-bindings"):
        transaction_dates.extend(sorted(
            (live / "canonical_receipts" / name).glob("date=*")))
    if len(state_dates) + len(tagged_dates) + len(transaction_dates) > limit:
        raise GateError(
            "UNFINISHED_BACKLOG_TOO_LARGE",
            f"more than {limit} daily/tagged state directories",
        )

    def parsed_date(path: pathlib.Path) -> str | None:
        match = re.fullmatch(r"date=(\d{4}-\d{2}-\d{2})", path.name)
        if match is None:
            return None
        try:
            date = _validate_date(match.group(1))
            if not _date_scope_allowed(date):
                _record_date_scope_block(
                    failures, date, "unfinished-state discovery")
                return None
            return date
        except GateError as exc:
            failures.append({
                "date": match.group(1),
                "code": "UNFINISHED_STATE_DATE_INVALID",
                "detail": f"{path}: {exc}",
            })
            return None

    for root in state_dates:
        date = parsed_date(root)
        if date is None:
            continue
        statuses = [root / "PREPARE_STATUS.json"]
        statuses.extend(sorted(root.glob("receipt=*/STATUS.json")))
        statuses = [path for path in statuses if path.exists()]
        nonterminal = False
        for path in statuses:
            try:
                status, _raw = _read_json(
                    path, MAX_AUDIT_BYTES, "unfinished daily status")
            except GateError as exc:
                failures.append({
                    "date": date,
                    "code": "UNFINISHED_STATE_INVALID",
                    "detail": f"{path}: {exc}",
                })
                nonterminal = True
                continue
            if status.get("state") == "V3_REFERENCE_PUBLISHED":
                if (status.get("schema_version")
                        == "research-v3-daily-status-v1"):
                    # Known pre-binding terminal receipts are not authority.
                    # Retry the idempotent publisher path so it can emit the
                    # real exact MANIFEST VersionId/size/SHA binding.
                    nonterminal = True
                else:
                    try:
                        _validate_local_terminal_status(
                            status, path, date, live)
                    except GateError as exc:
                        failures.append({
                            "date": date,
                            "code": "TERMINAL_STATE_INVALID",
                            "detail": f"{path}: {exc.detail}",
                        })
                        nonterminal = True
                    else:
                        # Local terminal state can never suppress an AWS retry.
                        # The main RUN path must exact-GET and hash the recorded
                        # VersionId before deciding this date is complete.
                        nonterminal = True
            else:
                nonterminal = True
        if nonterminal:
            candidates.add(date)

    for root in tagged_dates:
        date = parsed_date(root)
        if date is not None and any(root.glob("TAGGED-PENDING-*.json")):
            candidates.add(date)
    for root in transaction_dates:
        date = parsed_date(root)
        if date is not None:
            try:
                has_state = next(root.iterdir(), None) is not None
            except OSError as exc:
                failures.append({
                    "date": date,
                    "code": "UNFINISHED_STATE_INVALID",
                    "detail": f"{root}: {exc}",
                })
                has_state = True
            if has_state:
                candidates.add(date)
    return sorted(candidates)


def _base_env(home: pathlib.Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "USER": os.environ.get("USER", "ubuntu"),
        "LOGNAME": os.environ.get("LOGNAME", "ubuntu"),
        "PATH": os.environ.get(
            "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "AWS_PAGER": "",
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "AWS_CLI_HISTORY_FILE": "/dev/null",
        "AWS_CLI_HISTORY_ENABLED": "false",
        "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "true",
        "AWS_EC2_METADATA_DISABLED": "true",
        "GIT_CONFIG_SYSTEM": "/etc/kalshi-research-v3/gitconfig",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }


def tagger_env(home: pathlib.Path, credential_file: pathlib.Path,
               profile: str, region: str) -> dict[str, str]:
    env = _base_env(home)
    env.update({
        "AWS_SHARED_CREDENTIALS_FILE": str(credential_file),
        "AWS_PROFILE": profile,
        "AWS_DEFAULT_REGION": region,
        "AWS_REGION": region,
    })
    return env


PUBLISHER_SHELL = r"""
set -eu
umask 077
aws_expected=$1
expected_arn=$2
shift 2
if [ -n "${AWS_PROFILE:-}" ] || [ "${AWS_SHARED_CREDENTIALS_FILE:-}" != /dev/null ] || [ "${AWS_CONFIG_FILE:-}" != /dev/null ]; then
  echo 'PUBLISHER_CREDENTIAL_MODE_REFUSED: profile/shared config is not isolated' >&2
  exit 78
fi
if [ "${AWS_CLI_HISTORY_FILE:-}" != /dev/null ] || [ "${AWS_CLI_HISTORY_ENABLED:-}" != false ] || [ "${AWS_IGNORE_CONFIGURED_ENDPOINT_URLS:-}" != true ] || [ "${AWS_EC2_METADATA_DISABLED:-}" != true ]; then
  echo 'PUBLISHER_AWS_ENV_REFUSED: CLI config/history/endpoint/metadata hardening mismatch' >&2
  exit 78
fi
if [ -z "${AWS_ACCESS_KEY_ID:-}" ] || [ -z "${AWS_SECRET_ACCESS_KEY:-}" ]; then
  echo 'PUBLISHER_CREDENTIAL_MODE_REFUSED: isolated publisher keys missing' >&2
  exit 78
fi
case "$aws_expected" in
  /*) ;;
  *) echo 'PUBLISHER_AWS_BINARY_REFUSED: absolute path required' >&2; exit 78 ;;
esac
PATH="$(dirname "$aws_expected"):/usr/bin:/bin"
export PATH AWS_EC2_METADATA_DISABLED=true AWS_PAGER=
if [ "$(command -v aws)" != "$aws_expected" ]; then
  echo 'PUBLISHER_AWS_BINARY_REFUSED: PATH does not resolve audited binary' >&2
  exit 78
fi
actual_arn="$("$aws_expected" sts get-caller-identity --query Arn --output text)"
if [ "$actual_arn" != "$expected_arn" ]; then
  echo 'PUBLISHER_CALLER_REFUSED: caller ARN mismatch' >&2
  exit 78
fi
exec "$@"
""".strip()


def publisher_command(aws_cli: str, expected_arn: str,
                      command: list[str]) -> list[str]:
    return [
        "/bin/bash", "--noprofile", "--norc", "-c", PUBLISHER_SHELL,
        "research-v3-publisher", aws_cli, expected_arn, *command,
    ]


TAGGER_SHELL = r"""
set -eu
umask 077
aws_expected=$1
expected_arn=$2
expected_profile=$3
shift 3
if [ -n "${AWS_ACCESS_KEY_ID:-}" ] || [ -n "${AWS_SECRET_ACCESS_KEY:-}" ] || [ -n "${AWS_SESSION_TOKEN:-}" ]; then
  echo 'TAGGER_CREDENTIAL_MODE_REFUSED: inline publisher keys are forbidden' >&2
  exit 78
fi
if [ "${AWS_PROFILE:-}" != "$expected_profile" ] || [ -z "${AWS_SHARED_CREDENTIALS_FILE:-}" ]; then
  echo 'TAGGER_CREDENTIAL_MODE_REFUSED: fixed profile/shared file required' >&2
  exit 78
fi
if [ "${AWS_CONFIG_FILE:-}" != /dev/null ] || [ "${AWS_CLI_HISTORY_FILE:-}" != /dev/null ] || [ "${AWS_CLI_HISTORY_ENABLED:-}" != false ] || [ "${AWS_IGNORE_CONFIGURED_ENDPOINT_URLS:-}" != true ] || [ "${AWS_EC2_METADATA_DISABLED:-}" != true ]; then
  echo 'TAGGER_AWS_ENV_REFUSED: CLI config/history/endpoint/metadata hardening mismatch' >&2
  exit 78
fi
case "$AWS_SHARED_CREDENTIALS_FILE" in
  /*) ;;
  *) echo 'TAGGER_CREDENTIAL_MODE_REFUSED: absolute shared file required' >&2; exit 78 ;;
esac
case "$aws_expected" in
  /*) ;;
  *) echo 'TAGGER_AWS_BINARY_REFUSED: absolute path required' >&2; exit 78 ;;
esac
PATH="$(dirname "$aws_expected"):/usr/bin:/bin"
export PATH AWS_EC2_METADATA_DISABLED=true AWS_PAGER=
if [ "$(command -v aws)" != "$aws_expected" ]; then
  echo 'TAGGER_AWS_BINARY_REFUSED: PATH does not resolve audited binary' >&2
  exit 78
fi
actual_arn="$("$aws_expected" sts get-caller-identity --query Arn --output text)"
if [ "$actual_arn" != "$expected_arn" ]; then
  echo 'TAGGER_CALLER_REFUSED: caller ARN mismatch' >&2
  exit 78
fi
exec "$@"
""".strip()


def tagger_command(aws_cli: str, expected_arn: str,
                   command: list[str], *,
                   expected_profile: str = DEFAULT_TAGGER_PROFILE) -> list[str]:
    return [
        "/bin/bash", "--noprofile", "--norc", "-c", TAGGER_SHELL,
        "research-v3-tagger", aws_cli, expected_arn, expected_profile,
        *command,
    ]


def _run(command: list[str], *, cwd: pathlib.Path, env: dict[str, str],
         label: str, timeout: int = 4 * 60 * 60) -> str:
    try:
        result = subprocess.run(
            command, cwd=str(cwd), env=env, capture_output=True, text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateError("COMMAND_FAILED", f"{label}: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()[-2000:]
        raise GateError(
            "COMMAND_FAILED",
            f"{label} rc={result.returncode}: {detail or 'no detail'}",
        )
    return result.stdout


def _run_expect_denied(command: list[str], *, cwd: pathlib.Path,
                       env: dict[str, str], label: str,
                       timeout: int = 120) -> str:
    """Require a real AWS authorization denial; other errors do not pass."""
    try:
        result = subprocess.run(
            command, cwd=str(cwd), env=env, capture_output=True, text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateError("COMMAND_FAILED", f"{label}: {exc}") from exc
    detail = (result.stderr or result.stdout or "").strip()
    if result.returncode == 0:
        raise GateError("POLICY_CANARY_UNEXPECTED_ALLOW", label)
    if re.search(
            r"AccessDenied|explicit deny|not authorized to perform",
            detail, re.IGNORECASE) is None:
        raise GateError(
            "POLICY_CANARY_INCONCLUSIVE",
            f"{label} rc={result.returncode}: {detail[-1000:] or 'no detail'}",
        )
    return detail[-1000:]


def _last_json(output: str, label: str) -> dict:
    try:
        value = json.loads(output.strip().splitlines()[-1])
    except (IndexError, TypeError, ValueError) as exc:
        raise GateError("COMMAND_OUTPUT_INVALID", f"{label}: {exc}") from exc
    if not isinstance(value, dict):
        raise GateError("COMMAND_OUTPUT_INVALID", f"{label}: root is not an object")
    return value


def _json_document(output: str, label: str) -> dict:
    """Parse one complete JSON document, including AWS CLI pretty output."""
    try:
        value = json.loads(output)
    except (TypeError, ValueError) as exc:
        raise GateError("COMMAND_OUTPUT_INVALID", f"{label}: {exc}") from exc
    if not isinstance(value, dict):
        raise GateError("COMMAND_OUTPUT_INVALID", f"{label}: root is not an object")
    return value


def _data_env(args) -> dict[str, str]:
    env = _base_env(pathlib.Path(args.home))
    env.update({
        "RAW_ROOT": str(pathlib.Path(args.raw_root).absolute()),
        "WAREHOUSE_ROOT": str(pathlib.Path(args.warehouse_root).absolute()),
        "ARCHIVE_ROOT": str(
            pathlib.Path(args.warehouse_root).absolute() / "facts"),
    })
    return env


def _verify_seal(date: str, args) -> None:
    # Only the four bounded legacy migrations may authorize a pruned-local-raw
    # day from its full-v2 seal, retained facts, and exact quality receipts.
    # Every future date keeps the producer's ordinary seal verifier.
    import forward_canonical_receipts as fcr
    if date in fcr.LEGACY_MIGRATION_DATES:
        _run([
            os.path.abspath(args.python),
            str(ROOT / "tools" / "forward_canonical_receipts.py"),
            "verify-historical-authority",
            "--date", date,
            "--bucket", DEFAULT_BUCKET,
            "--prefix", DEFAULT_PREFIX,
            "--raw-root", str(pathlib.Path(args.raw_root).absolute()),
            "--warehouse-root",
            str(pathlib.Path(args.warehouse_root).absolute()),
            "--quality-dir", str(pathlib.Path(args.quality_dir).absolute()),
        ], cwd=ROOT, env=_data_env(args),
            label=f"historical metadata authority {date}")
        return
    _run([
        os.path.abspath(args.python), str(ROOT / "tools" / "export_day.py"),
        "--date", date, "--verify-seal",
    ], cwd=ROOT, env=_data_env(args), label=f"seal verification {date}")


def _publisher_run(command: list[str], *, args,
                   publisher_environment: dict[str, str], label: str,
                   timeout: int = 4 * 60 * 60) -> str:
    _require_arms(args)
    aws = os.path.abspath(args.aws_cli)
    return _run(
        publisher_command(aws, args.publisher_principal, command),
        cwd=ROOT, env=publisher_environment, label=label, timeout=timeout)


def _validate_prepared_result(output: str, *, date: str,
                              prepared_root: pathlib.Path):
    result = _last_json(output, f"reference prepare {date}")
    required = {
        "schema_version", "state", "date", "release_id", "prepared_plan",
        "prepared_plan_sha256", "prepared_plan_size",
        "reference_set_sha256", "s3_writes", "rfq",
    }
    release_pattern = (
        re.escape(date) + r"__v3ref__seal-[0-9a-f]{8}__pub-[0-9a-f]{16}")
    if (set(result) != required
            or result.get("schema_version") != PREPARED_PLAN_SCHEMA
            or result.get("state") != PREPARED_PLAN_STATE
            or result.get("date") != date
            or not isinstance(result.get("release_id"), str)
            or re.fullmatch(release_pattern, result["release_id"]) is None
            or not isinstance(result.get("prepared_plan_sha256"), str)
            or SHA_RE.fullmatch(result["prepared_plan_sha256"]) is None
            or not isinstance(result.get("reference_set_sha256"), str)
            or SHA_RE.fullmatch(result["reference_set_sha256"]) is None
            or not isinstance(result.get("prepared_plan_size"), int)
            or isinstance(result.get("prepared_plan_size"), bool)
            or result["prepared_plan_size"] < 1
            or result["prepared_plan_size"] > MAX_REFERENCE_PREPARED_PLAN_BYTES
            or result.get("s3_writes") != 0
            or result.get("rfq") != "OFF"):
        raise GateError(
            "REFERENCE_PREPARE_OUTPUT_INVALID",
            "publisher prepare result differs from the fixed zero-write contract",
        )
    try:
        root = pathlib.Path(prepared_root).resolve(strict=True)
        plan = pathlib.Path(result["prepared_plan"])
        resolved = plan.resolve(strict=True)
    except (KeyError, OSError, TypeError) as exc:
        raise GateError("REFERENCE_PREPARE_OUTPUT_INVALID", str(exc)) from exc
    expected_name = f"PREPARED-{result['prepared_plan_sha256']}.json"
    if (not plan.is_absolute() or plan.absolute() != resolved
            or resolved.parent != root or resolved.name != expected_name):
        raise GateError(
            "REFERENCE_PREPARE_OUTPUT_INVALID",
            "prepared plan is outside its receipt-scoped content-addressed root",
        )
    raw = _read_regular(
        resolved, MAX_REFERENCE_PREPARED_PLAN_BYTES, "prepared reference plan")
    if (len(raw) != result["prepared_plan_size"]
            or hashlib.sha256(raw).hexdigest()
            != result["prepared_plan_sha256"]):
        raise GateError(
            "REFERENCE_PREPARE_OUTPUT_INVALID",
            "prepared plan bytes do not match publisher result",
        )
    return resolved, result


def _validate_manifest_commit_result(output: str, *, date: str,
                                     prepared_plan_sha256: str) -> dict:
    result = _last_json(output, f"reference manifest commit {date}")
    required = {
        "schema_version", "state", "release_id", "manifest_object",
        "data_uploads", "rfq", "prepared_plan_sha256",
    }
    release_id = result.get("release_id")
    release_pattern = (
        re.escape(date) + r"__v3ref__seal-[0-9a-f]{8}__pub-[0-9a-f]{16}")
    if (set(result) != required
            or result.get("schema_version")
            != MANIFEST_COMMIT_RESULT_SCHEMA
            or result.get("state") not in {
                "REFERENCE_MANIFEST_COMMITTED",
                "REFERENCE_MANIFEST_ALREADY_COMMITTED"}
            or not isinstance(release_id, str)
            or re.fullmatch(release_pattern, release_id) is None
            or result.get("prepared_plan_sha256")
            != prepared_plan_sha256
            or result.get("data_uploads") != 0
            or result.get("rfq") != "OFF"):
        raise GateError(
            "REFERENCE_COMMIT_OUTPUT_INVALID",
            "publisher commit result differs from the fixed manifest-only contract",
        )
    try:
        binding = _validate_manifest_object_binding(
            result.get("manifest_object"), release_id, date)
    except GateError as exc:
        raise GateError("REFERENCE_COMMIT_OUTPUT_INVALID", exc.detail) from exc
    normalized = dict(result)
    normalized["manifest_object"] = binding
    return normalized


def _verify_terminal_status(status: dict, status_path: pathlib.Path,
                            date: str, receipt_sha: str,
                            live_dir: pathlib.Path,
                            audit_root: pathlib.Path, tagger_arn: str, *,
                            args, publisher_environment: dict[str, str]):
    """Authenticate one terminal STATUS against exact MANIFEST bytes."""
    tagged, binding = _validate_local_terminal_status(
        status, status_path, date, live_dir)
    if status["durable_receipt_set_sha256"] != receipt_sha:
        raise GateError(
            "TERMINAL_STATE_INVALID", "terminal durable receipt differs")
    completed_tagged = _validate_completed_state(
        date, receipt_sha, live_dir, audit_root, tagger_arn)
    if completed_tagged != tagged:
        raise GateError(
            "TERMINAL_STATE_INVALID", "terminal tagged index differs")

    aws = os.path.abspath(args.aws_cli)
    head = _json_document(_publisher_run([
        aws, "s3api", "head-object", "--bucket", binding["bucket"],
        "--key", binding["key"], "--version-id", binding["VersionId"],
        "--output", "json",
    ], args=args, publisher_environment=publisher_environment,
        label=f"terminal MANIFEST exact HEAD {date}", timeout=20 * 60),
        f"terminal MANIFEST exact HEAD {date}")
    if (head.get("VersionId") != binding["VersionId"]
            or head.get("ContentLength") != binding["size"]
            or head.get("DeleteMarker") is True):
        raise GateError(
            "TERMINAL_MANIFEST_MISMATCH",
            "HeadObject does not reproduce the recorded exact MANIFEST",
        )
    with tempfile.TemporaryDirectory(
            prefix="research-v3-terminal-manifest-") as root:
        output = pathlib.Path(root) / "MANIFEST.json"
        got = _json_document(_publisher_run([
            aws, "s3api", "get-object", "--bucket", binding["bucket"],
            "--key", binding["key"], "--version-id", binding["VersionId"],
            "--output", "json", str(output),
        ], args=args, publisher_environment=publisher_environment,
            label=f"terminal MANIFEST exact GET {date}", timeout=20 * 60),
            f"terminal MANIFEST exact GET {date}")
        raw = _read_regular(
            output, MAX_REFERENCE_MANIFEST_BYTES, "terminal MANIFEST bytes")
    if (got.get("VersionId") != binding["VersionId"]
            or got.get("ContentLength") != binding["size"]
            or len(raw) != binding["size"]
            or hashlib.sha256(raw).hexdigest() != binding["sha256"]):
        raise GateError(
            "TERMINAL_MANIFEST_MISMATCH",
            "exact GET bytes do not reproduce terminal size/SHA/VersionId",
        )
    try:
        manifest = json.loads(raw, object_pairs_hook=_json_no_duplicates)
        if not isinstance(manifest, dict):
            raise ValueError("manifest root is not an object")
        import research_reference as reference_contract
        descriptor = reference_contract.validate_manifest(
            manifest, status["release_id"])
    except GateError:
        raise
    except Exception as exc:
        raise GateError(
            "TERMINAL_MANIFEST_INVALID", str(exc)) from exc
    tagged_index, _raw = _read_json(
        tagged, MAX_INDEX_BYTES, "terminal tagged index")
    expected_receipt_object = tagged_index["receipt_object"]
    expected_normalized = {
        "bucket": expected_receipt_object["bucket"],
        "key": expected_receipt_object["key"],
        "version_id": expected_receipt_object["VersionId"],
        "size": expected_receipt_object["size"],
        "sha256": expected_receipt_object["sha256"],
    }
    if (descriptor.get("date") != date
            or descriptor.get("release_id") != status["release_id"]
            or descriptor.get("rfq_included") is not False
            or descriptor.get("canonical_receipt_set_sha256")
            != tagged_index["receipt_set_sha256"]
            or descriptor.get("receipt_object") != expected_normalized):
        raise GateError(
            "TERMINAL_MANIFEST_INVALID",
            "MANIFEST does not bind the completed tagged receipt exactly",
        )
    return tagged, binding


MAX_PATROL_CANARY_BYTES = 64 * 1024 * 1024
MAX_VERSION_HISTORY_PAGES = 64
MAX_VERSION_HISTORY_ROWS = 4096
MAX_VERSION_SHA_CANDIDATES = 32
MAX_VERSION_RESOLUTION_BYTES = 64 * 1024 * 1024 * 1024
MAX_GENERATION_WITNESS_BYTES = 8 * 1024 * 1024


def _valid_version_id(value: object) -> bool:
    return (isinstance(value, str) and bool(value.strip())
            and value.strip().lower() != "null")


def _file_attestation(path: pathlib.Path) -> tuple[int, str]:
    path = pathlib.Path(path).absolute()
    if not hasattr(os, "O_NOFOLLOW"):
        raise GateError("LOCAL_SAFETY_UNAVAILABLE", "O_NOFOLLOW is required")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW |
                     getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise GateError("CATALOG_SYNC_INVALID", str(exc)) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise GateError("CATALOG_SYNC_INVALID", str(path))
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
        after = os.fstat(fd)
        if size != before.st_size or _fingerprint(before) != _fingerprint(after):
            raise GateError("LOCAL_INPUT_CHANGED", str(path))
        return size, digest.hexdigest()
    finally:
        os.close(fd)


def _exact_remote_catalog_bytes(
        key: str, version_id: str, output: pathlib.Path, *, args,
        publisher_environment: dict[str, str]) -> tuple[int, str]:
    aws = os.path.abspath(args.aws_cli)
    response = _json_document(_publisher_run([
        aws, "s3api", "get-object", "--bucket", DEFAULT_BUCKET, "--key", key,
        "--version-id", version_id, str(output),
    ], args=args, publisher_environment=publisher_environment,
        label=f"publication snapshot exact readback {key}", timeout=20 * 60),
        f"publication snapshot exact readback {key}")
    size, digest = _file_attestation(output)
    if (response.get("VersionId") != version_id
            or response.get("ContentLength") != size):
        raise GateError(
            "EXACT_VERSION_RESPONSE_INVALID",
            f"get-object response does not bind {key}@{version_id}")
    return size, digest


def _load_forward_resolution_targets(date: str, aux_bundle: pathlib.Path,
                                     args):
    """Validate aux and return the witness contract plus hash-bound corrections."""
    tools_dir = str(ROOT / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    try:
        import canonical_receipts as cr
        import forward_canonical_receipts as fcr
    except ImportError as exc:
        raise GateError("FORWARD_SNAPSHOT_INVALID", str(exc)) from exc
    try:
        seal, binding, *_unused = cr._authoritative_day_inputs(
            date, str(pathlib.Path(args.raw_root).absolute()),
            str(pathlib.Path(args.warehouse_root).absolute()))
        descriptor, rows = fcr.load_forward_auxiliary_set(
            str(aux_bundle), date, DEFAULT_BUCKET, DEFAULT_PREFIX,
            seal, binding["sha256"])
    except cr.ReceiptError as exc:
        raise GateError("FORWARD_SNAPSHOT_INVALID", str(exc)) from exc
    late_targets = fcr.forward_version_resolution_targets(rows)
    contract = descriptor.get("generation_witness_contract")
    if (not isinstance(contract, dict)
            or contract.get("discovery_rule")
            != "EXACTLY_ONE_CONTENT_ADDRESSED_WITNESS"
            or contract.get("read_rule")
            != "HEAD_VERSIONID_THEN_EXACT_GET_NO_FALLBACK"):
        raise GateError(
            "FORWARD_SNAPSHOT_INVALID", "generation witness contract missing")
    return cr, fcr, seal, binding, descriptor, rows, late_targets


def _history_time(value: object) -> dt.datetime:
    if not isinstance(value, str) or not value.strip():
        raise GateError("VERSION_HISTORY_INVALID", "LastModified missing")
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise GateError("VERSION_HISTORY_INVALID", str(exc)) from exc
    if parsed.tzinfo is None:
        raise GateError("VERSION_HISTORY_INVALID", "LastModified is naive")
    return parsed.astimezone(dt.timezone.utc)


def _list_exact_version_history(
        key: str, bucket: str, *, args,
        publisher_environment: dict[str, str]) -> tuple[dict, dict]:
    """Read one bounded, manually paginated S3 history for an exact key."""
    aws = os.path.abspath(args.aws_cli)
    versions: list[dict] = []
    delete_markers: list[dict] = []
    pages = 0
    rows_seen = 0
    key_marker = None
    version_marker = None
    while True:
        pages += 1
        if pages > MAX_VERSION_HISTORY_PAGES:
            raise GateError("VERSION_HISTORY_BOUNDED", f"too many pages {key}")
        command = [
            aws, "s3api", "list-object-versions", "--bucket", bucket,
            "--prefix", key, "--max-keys", "1000", "--no-paginate",
            "--output", "json",
        ]
        if key_marker is not None:
            command.extend(["--key-marker", key_marker])
            if version_marker is not None:
                command.extend(["--version-id-marker", version_marker])
        page = _json_document(_publisher_run(
            command, args=args, publisher_environment=publisher_environment,
            label=f"bounded exact-key history {key} page {pages}", timeout=120),
            f"bounded exact-key history {key} page {pages}")
        truncated = page.get("IsTruncated", False)
        if not isinstance(truncated, bool):
            raise GateError("VERSION_HISTORY_INVALID", f"truncation {key}")
        for field, destination in (
                ("Versions", versions), ("DeleteMarkers", delete_markers)):
            page_rows = page.get(field) or []
            if not isinstance(page_rows, list):
                raise GateError(
                    "VERSION_HISTORY_INVALID", f"{field} {key}")
            rows_seen += len(page_rows)
            if rows_seen > MAX_VERSION_HISTORY_ROWS:
                raise GateError(
                    "VERSION_HISTORY_BOUNDED", f"too many rows {key}")
            for item in page_rows:
                if not isinstance(item, dict):
                    raise GateError("VERSION_HISTORY_INVALID", f"row {key}")
                if item.get("Key") != key:
                    continue
                version_id = item.get("VersionId")
                if not _valid_version_id(version_id):
                    raise GateError(
                        "VERSION_HISTORY_INVALID", f"version {key}")
                _history_time(item.get("LastModified"))
                if field == "Versions" and (
                        not isinstance(item.get("Size"), int)
                        or isinstance(item.get("Size"), bool)
                        or item["Size"] < 0):
                    raise GateError(
                        "VERSION_HISTORY_INVALID", f"size {key}")
                destination.append(dict(item))
        if not truncated:
            break
        next_key = page.get("NextKeyMarker")
        next_version = page.get("NextVersionIdMarker")
        if (not isinstance(next_key, str) or not next_key
                or (next_key, next_version) == (key_marker, version_marker)
                or (next_version is not None
                    and not isinstance(next_version, str))):
            raise GateError("VERSION_HISTORY_INVALID", f"pagination {key}")
        key_marker, version_marker = next_key, next_version

    version_ids = [row["VersionId"] for row in versions + delete_markers]
    if len(version_ids) != len(set(version_ids)):
        raise GateError("VERSION_HISTORY_INVALID", f"duplicate versions {key}")
    history = {
        "IsTruncated": False,
        "Versions": versions,
        "DeleteMarkers": delete_markers,
    }
    evidence = {
        "history_pages": pages,
        "history_rows_seen": rows_seen,
        "versions_seen": len(versions),
        "delete_markers_seen": len(delete_markers),
    }
    return history, evidence


def _discover_exact_generation_witness(
        date: str, descriptor: dict, seal_binding: dict,
        temp: pathlib.Path, *, canonical_receipts,
        forward_receipts, args,
        publisher_environment: dict[str, str]) -> tuple[dict, dict]:
    """Discover and exact-read the one content-addressed generation witness.

    This is intentionally not a version-history or timestamp resolver.  A
    missing, multiple, truncated, noncanonical, or otherwise invalid witness
    blocks the day with no fallback to mutable catalog/dim paths.
    """
    contract = descriptor.get("generation_witness_contract")
    if not isinstance(contract, dict):
        raise GateError(
            "GENERATION_WITNESS_REQUIRED", "witness contract is missing")
    prefix = contract.get("key_prefix")
    if not isinstance(prefix, str) or not prefix:
        raise GateError(
            "GENERATION_WITNESS_REQUIRED", "witness prefix is invalid")
    aws = os.path.abspath(args.aws_cli)
    listing = _json_document(_publisher_run([
        aws, "s3api", "list-objects-v2", "--bucket", DEFAULT_BUCKET,
        "--prefix", prefix, "--max-keys", "1000", "--no-paginate",
        "--output", "json",
    ], args=args, publisher_environment=publisher_environment,
        label=f"generation witness discovery {date}", timeout=120),
        f"generation witness discovery {date}")
    contents = listing.get("Contents") or []
    if (listing.get("IsTruncated") is not False
            or not isinstance(contents, list)
            or len(contents) != 1
            or listing.get("CommonPrefixes") not in (None, [])):
        raise GateError(
            "GENERATION_WITNESS_REQUIRED",
            f"exactly one complete witness listing is required for {date}")
    listed = contents[0]
    if not isinstance(listed, dict):
        raise GateError("GENERATION_WITNESS_INVALID", "listing row malformed")
    key = listed.get("Key")
    match = re.fullmatch(
        re.escape(prefix)
        + r"witness-([0-9a-f]{64})\.json",
        key if isinstance(key, str) else "")
    listed_size = listed.get("Size")
    if (match is None or not isinstance(listed_size, int)
            or isinstance(listed_size, bool) or listed_size <= 0
            or listed_size > MAX_GENERATION_WITNESS_BYTES):
        raise GateError(
            "GENERATION_WITNESS_INVALID", "content-addressed listing invalid")
    key_digest = match.group(1)
    head = _json_document(_publisher_run([
        aws, "s3api", "head-object", "--bucket", DEFAULT_BUCKET,
        "--key", key, "--output", "json",
    ], args=args, publisher_environment=publisher_environment,
        label=f"generation witness exact version {date}", timeout=120),
        f"generation witness exact version {date}")
    version_id = head.get("VersionId")
    head_size = head.get("ContentLength")
    if (not _valid_version_id(version_id)
            or not isinstance(head_size, int) or isinstance(head_size, bool)
            or head_size != listed_size or head_size <= 0
            or head_size > MAX_GENERATION_WITNESS_BYTES):
        raise GateError(
            "GENERATION_WITNESS_INVALID", "witness HEAD binding invalid")
    modified = head.get("LastModified")
    _history_time(modified)
    output = temp / "generation-witness.json"
    try:
        size, digest = _exact_remote_catalog_bytes(
            key, version_id, output, args=args,
            publisher_environment=publisher_environment)
        raw = _read_regular(
            output, MAX_GENERATION_WITNESS_BYTES, "generation witness")
    finally:
        try:
            output.unlink()
        except FileNotFoundError:
            pass
    if (size != head_size or len(raw) != head_size
            or digest != key_digest
            or hashlib.sha256(raw).hexdigest() != digest):
        raise GateError(
            "GENERATION_WITNESS_INVALID", "witness byte binding invalid")
    try:
        payload = json.loads(raw, object_pairs_hook=_json_no_duplicates)
    except GateError as exc:
        raise GateError("GENERATION_WITNESS_INVALID", exc.detail) from exc
    except (UnicodeDecodeError, ValueError) as exc:
        raise GateError("GENERATION_WITNESS_INVALID", str(exc)) from exc
    if not isinstance(payload, dict):
        raise GateError("GENERATION_WITNESS_INVALID", "witness root invalid")
    if (raw != forward_receipts.generation_witness_bytes(payload)
            or forward_receipts.expected_generation_witness_key(
                DEFAULT_PREFIX, date, digest) != key):
        raise GateError(
            "GENERATION_WITNESS_INVALID", "witness wire/key form invalid")
    try:
        forward_receipts.validate_generation_witness(
            payload, date, DEFAULT_BUCKET, DEFAULT_PREFIX,
            seal_binding["sha256"], seal_binding["size"],
            contract["seal_sealed_at_utc"])
    except canonical_receipts.ReceiptError as exc:
        raise GateError("GENERATION_WITNESS_INVALID", str(exc)) from exc
    envelope = {
        "bucket": DEFAULT_BUCKET, "key": key, "VersionId": version_id,
        "size": size, "sha256": digest,
        "LastModified": canonical_receipts._canonical_utc(
            modified, "generation witness LastModified"),
    }
    return payload, envelope


def _resolve_forward_exact_version(
        row: dict, number: int, temp: pathlib.Path, *, args,
        publisher_environment: dict[str, str],
        download_budget: dict[str, int]) -> dict:
    """Resolve original canonical key to an exact SHA-matching VersionId."""
    key = row["key"]
    expected = (row["size"], row["sha256"])
    local = row.get("_local_path")
    if local is not None and _file_attestation(pathlib.Path(local)) != expected:
        raise GateError("FORWARD_SNAPSHOT_INVALID", f"local witness {key}")
    aws = os.path.abspath(args.aws_cli)
    current = None
    exact_gets = 0

    def exact_attestation(version_id: str, output: pathlib.Path):
        next_total = download_budget["bytes"] + expected[0]
        if next_total > MAX_VERSION_RESOLUTION_BYTES:
            raise GateError("VERSION_HISTORY_BOUNDED", "global download cap")
        download_budget["bytes"] = next_total
        try:
            return _exact_remote_catalog_bytes(
                key, version_id, output, args=args,
                publisher_environment=publisher_environment)
        finally:
            try:
                output.unlink()
            except FileNotFoundError:
                pass
    try:
        current = _json_document(_publisher_run([
            aws, "s3api", "head-object", "--bucket", DEFAULT_BUCKET,
            "--key", key, "--output", "json",
        ], args=args, publisher_environment=publisher_environment,
            label=f"current exact-version candidate {key}", timeout=120),
            f"current exact-version candidate {key}")
    except GateError:
        current = None
    current_version = current.get("VersionId") if current else None
    if current is not None:
        if (not _valid_version_id(current_version)
                or current.get("LastModified") is None):
            raise GateError("VERSION_HISTORY_INVALID", f"current {key}")
        _history_time(current["LastModified"])
        if current.get("ContentLength") == expected[0]:
            probe = temp / f"current-{number}.bin"
            exact_gets += 1
            if exact_attestation(current_version, probe) == expected:
                evidence = {
                    "logical_source_key": row["logical_source_key"],
                    "key": key, "current_version_id": current_version,
                    "current_exact_sha256_match": True,
                    "history_pages": 0, "versions_seen": 1,
                    "same_size_candidates": 1, "exact_gets": exact_gets,
                    "matching_version_ids": [current_version],
                    "selected_version_id": current_version,
                    "selection_rule":
                        "CURRENT_EXACT_SHA256_ELSE_VERIFIED_MATCH_MAX_LASTMODIFIED_VERSIONID",
                }
                return {
                    "logical_source_key": row["logical_source_key"],
                    "bucket": row["bucket"], "key": key,
                    "size": expected[0], "sha256": expected[1],
                    "VersionId": current_version,
                    "LastModified": current["LastModified"],
                    "resolution": "CURRENT_EXACT_SHA256_MATCH",
                    "resolver_evidence": evidence,
                }

    history, history_evidence = _list_exact_version_history(
        key, row["bucket"], args=args,
        publisher_environment=publisher_environment)
    versions = history["Versions"]
    candidates = [item for item in versions if item["Size"] == expected[0]]
    if len(candidates) > MAX_VERSION_SHA_CANDIDATES:
        raise GateError("VERSION_HISTORY_BOUNDED", f"too many candidates {key}")
    matches = []
    for index, item in enumerate(candidates):
        version_id = item["VersionId"]
        if version_id == current_version and exact_gets:
            continue
        output = temp / f"history-{number}-{index}.bin"
        exact_gets += 1
        if exact_attestation(version_id, output) == expected:
            matches.append(item)
    if not matches:
        raise GateError("VERSION_SHA256_NOT_FOUND", key)
    matches.sort(key=lambda item: (
        _history_time(item["LastModified"]), item["VersionId"]))
    selected = matches[-1]
    matching_ids = sorted(item["VersionId"] for item in matches)
    evidence = {
        "logical_source_key": row["logical_source_key"],
        "key": key, "current_version_id": current_version,
        "current_exact_sha256_match": False,
        **history_evidence,
        "same_size_candidates": len(candidates), "exact_gets": exact_gets,
        "matching_version_ids": matching_ids,
        "selected_version_id": selected["VersionId"],
        "selection_rule":
            "CURRENT_EXACT_SHA256_ELSE_VERIFIED_MATCH_MAX_LASTMODIFIED_VERSIONID",
    }
    return {
        "logical_source_key": row["logical_source_key"],
        "bucket": row["bucket"], "key": key,
        "size": expected[0], "sha256": expected[1],
        "VersionId": selected["VersionId"],
        "LastModified": selected["LastModified"],
        "resolution": "HISTORICAL_EXACT_SHA256_MATCH",
        "resolver_evidence": evidence,
    }


def _sync_catalog_and_freeze(
        date: str, args, *,
        publisher_environment: dict[str, str]) -> dict:
    """Freeze controls and bind original keys through one exact witness."""
    warehouse = pathlib.Path(args.warehouse_root).absolute()
    aux_root = (pathlib.Path(args.live_dir).absolute()
                / "canonical_receipts" / "forward-aux")
    freeze_output = _run([
        os.path.abspath(args.python),
        str(ROOT / "tools" / "forward_canonical_receipts.py"),
        "freeze-forward", "--date", date, "--bucket", DEFAULT_BUCKET,
        "--prefix", DEFAULT_PREFIX, "--raw-root",
        str(pathlib.Path(args.raw_root).absolute()),
        "--warehouse-root", str(warehouse), "--quality-dir",
        str(pathlib.Path(args.quality_dir).absolute()),
        "--aux-root", str(aux_root),
    ], cwd=ROOT, env=_data_env(args),
        label=f"freeze small publication controls {date}", timeout=15 * 60)
    frozen = _last_json(freeze_output, "freeze-forward")
    aux_value = frozen.get("path")
    if (frozen.get("state") != "FORWARD_AUXILIARY_SET_FROZEN"
            or frozen.get("date") != date or frozen.get("s3_writes") != 0
            or not isinstance(aux_value, str)):
        raise GateError("FORWARD_FREEZE_INVALID", date)
    try:
        aux_bundle = pathlib.Path(aux_value).resolve(strict=True)
        aux_bundle.relative_to(aux_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise GateError("FORWARD_FREEZE_INVALID", str(exc)) from exc
    cr, fcr, _seal, seal_binding, descriptor, rows, late_targets = \
        _load_forward_resolution_targets(date, aux_bundle, args)
    with tempfile.TemporaryDirectory(
            prefix=f"research-v3-version-resolve-{date}-") as temp_value:
        temp = pathlib.Path(temp_value)
        download_budget = {"bytes": 0}
        witness_payload, witness_object = _discover_exact_generation_witness(
            date, descriptor, seal_binding, temp,
            canonical_receipts=cr, forward_receipts=fcr, args=args,
            publisher_environment=publisher_environment)
        late_resolutions = list(
            _resolve_forward_exact_version(
                row, number, temp, args=args,
                publisher_environment=publisher_environment,
                download_budget=download_budget)
            for number, row in enumerate(late_targets))
    version_root = (pathlib.Path(args.live_dir).absolute()
                    / "canonical_receipts" / "forward-version-bindings")
    try:
        version_path, version_payload = fcr.write_forward_version_binding(
            str(version_root), descriptor, rows, date, DEFAULT_BUCKET,
            DEFAULT_PREFIX, witness_payload, witness_object,
            late_resolutions)
    except fcr.cr.ReceiptError as exc:
        raise GateError("FORWARD_VERSION_BINDING_INVALID", str(exc)) from exc
    result = dict(frozen)
    result["version_binding"] = version_path
    result["version_binding_sha256"] = \
        version_payload["version_binding_sha256"]
    result["catalog_dim_resolution"] = \
        "ZERO_COPY_EXACT_GENERATION_WITNESS_BOUND"
    return result


def ensure_durable(date: str, args, *,
                   publisher_environment: dict[str, str]) -> pathlib.Path:
    """Build the immutable durable receipt when the daily producer is absent."""
    live_dir = pathlib.Path(args.live_dir).absolute()
    receipt_root = (pathlib.Path(args.receipt_root).absolute()
                    if args.receipt_root else None)
    existing = _find_durable(date, live_dir, receipt_root)
    if existing is not None:
        _validate_durable_index(existing, date)
        return existing

    python = os.path.abspath(args.python)
    aws = os.path.abspath(args.aws_cli)
    raw_root = str(pathlib.Path(args.raw_root).absolute())
    warehouse_root = str(pathlib.Path(args.warehouse_root).absolute())
    quality_dir = str(pathlib.Path(args.quality_dir).absolute())
    canonical = live_dir / "canonical_receipts"
    aux_root = canonical / "forward-aux"

    _verify_seal(date, args)
    frozen = _sync_catalog_and_freeze(
        date, args, publisher_environment=publisher_environment)
    aux_value = frozen.get("path")
    if (frozen.get("state") != "FORWARD_AUXILIARY_SET_FROZEN"
            or frozen.get("date") != date
            or frozen.get("s3_writes") != 0
            or not isinstance(aux_value, str)):
        raise GateError("FORWARD_FREEZE_INVALID", date)
    try:
        aux_bundle = pathlib.Path(aux_value).resolve(strict=True)
        aux_bundle.relative_to(aux_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise GateError("FORWARD_FREEZE_INVALID", str(exc)) from exc
    version_value = frozen.get("version_binding")
    version_root = canonical / "forward-version-bindings"
    try:
        if not isinstance(version_value, str):
            raise ValueError("version binding path missing")
        version_binding = pathlib.Path(version_value).resolve(strict=True)
        version_binding.relative_to(version_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise GateError("FORWARD_VERSION_BINDING_INVALID", str(exc)) from exc

    planned = _last_json(_run([
        python, str(ROOT / "tools" / "forward_canonical_receipts.py"),
        "plan-forward", "--date", date, "--bucket", DEFAULT_BUCKET,
        "--prefix", DEFAULT_PREFIX, "--raw-root", raw_root,
        "--warehouse-root", warehouse_root, "--quality-dir", quality_dir,
        "--aux-bundle", str(aux_bundle),
        "--version-binding", str(version_binding),
    ], cwd=ROOT, env=_data_env(args), label=f"plan forward {date}"),
        "plan-forward")
    if (planned.get("state") != "FORWARD_INVENTORY_PLANNED"
            or planned.get("date") != date or planned.get("s3_writes") != 0
            or not isinstance(planned.get("objects"), int)
            or planned["objects"] <= 0):
        raise GateError("FORWARD_PLAN_INVALID", date)

    common = [
        "--date", date, "--bucket", DEFAULT_BUCKET, "--prefix", DEFAULT_PREFIX,
        "--raw-root", raw_root, "--warehouse-root", warehouse_root,
        "--quality-dir", quality_dir, "--aux-bundle", str(aux_bundle),
        "--version-binding", str(version_binding),
        "--aws-cli", aws, "--operator-approved",
    ]
    _verify_seal(date, args)
    controls = _last_json(_publisher_run([
        python, str(ROOT / "tools" / "canonical_receipt_control.py"),
        "sync-controls", *common,
        "--output-root", str(canonical / "controls"),
    ], args=args, publisher_environment=publisher_environment,
        label=f"sync canonical controls {date}"), "sync-controls")
    if (controls.get("large_data_upload_bytes") != 0
            or not isinstance(controls.get("index"), str)):
        raise GateError("CONTROL_SYNC_INVALID", date)

    preflight = _last_json(_publisher_run([
        python, str(ROOT / "tools" / "forward_canonical_receipts.py"),
        "shadow-forward", "--date", date, "--bucket", DEFAULT_BUCKET,
        "--prefix", DEFAULT_PREFIX, "--raw-root", raw_root,
        "--warehouse-root", warehouse_root, "--quality-dir", quality_dir,
        "--aux-bundle", str(aux_bundle), "--aws-cli", aws,
        "--version-binding", str(version_binding),
        "--metadata-only", "--workers", str(CANONICAL_VERIFY_WORKERS),
        "--output-root",
        str(canonical / "forward-metadata-preflight"),
    ], args=args, publisher_environment=publisher_environment,
        label=f"inventory metadata preflight {date}"),
        "metadata preflight")
    if (preflight.get("state") != "METADATA_PREFLIGHT_VERIFIED"
            or preflight.get("failures") != 0):
        raise GateError("METADATA_PREFLIGHT_INVALID", date)

    _verify_seal(date, args)
    # The immutable aux bundle was snapshotted/revalidated under short
    # generation locks above.  Full exact-VersionId verification can take
    # hours and must never hold producer locks across network I/O.
    durable = _last_json(_publisher_run([
        python, str(ROOT / "tools" / "canonical_receipt_control.py"),
        "publish-receipt", *common,
        "--workers", str(CANONICAL_VERIFY_WORKERS),
        "--output-root", str(canonical / "durable"),
    ], args=args, publisher_environment=publisher_environment,
        label=f"publish durable receipt {date}"), "publish-receipt")
    index_value = durable.get("index")
    if (durable.get("state") != "DURABLE_RECEIPT_VERIFIED"
            or not isinstance(index_value, str)):
        raise GateError("DURABLE_PUBLICATION_INVALID", date)
    index_path = pathlib.Path(index_value).absolute()
    expected_root = canonical / "durable" / f"date={date}"
    try:
        index_path.resolve(strict=True).relative_to(expected_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise GateError("DURABLE_PUBLICATION_INVALID", str(exc)) from exc
    _validate_durable_index(index_path, date)
    return index_path


def _identity(command: list[str], expected_arn: str, *, cwd: pathlib.Path,
              env: dict[str, str], label: str) -> dict:
    raw = _run(command, cwd=cwd, env=env, label=label, timeout=60)
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise GateError("CALLER_IDENTITY_INVALID", f"{label}: {exc}") from exc
    if (not isinstance(value, dict) or value.get("Arn") != expected_arn
            or not value.get("Account") or not value.get("UserId")):
        raise GateError(
            "CALLER_IDENTITY_MISMATCH",
            f"{label} ARN {value.get('Arn') if isinstance(value, dict) else None!r}",
        )
    return value


def _json_bytes(payload: dict) -> bytes:
    return (json.dumps(payload, sort_keys=True, indent=2,
                       ensure_ascii=True) + "\n").encode("utf-8")


def _atomic_bytes(path: pathlib.Path, raw: bytes) -> None:
    path = pathlib.Path(path).absolute()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    temp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        temp = None
    finally:
        if fd >= 0:
            os.close(fd)
        if temp is not None:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass


def _atomic_json(path: pathlib.Path, payload: dict) -> None:
    _atomic_bytes(path, _json_bytes(payload))


def _record_attempt_failure(status_path: pathlib.Path,
                            failure: dict) -> pathlib.Path:
    """Append failure evidence without replacing a terminal success."""
    status_path = pathlib.Path(status_path).absolute()
    attempt, _digest, _size = _write_content_addressed(
        status_path.parent / "attempts", "failure", failure)
    terminal = False
    if status_path.exists():
        try:
            old, _raw = _read_json(
                status_path, MAX_AUDIT_BYTES, "daily status")
        except GateError:
            # The content-addressed attempt above is the durable failure record.
            # Never destroy malformed or suspicious prior bytes while handling
            # another error, and never let error recording abort the date loop.
            return attempt
        terminal = old.get("state") == "V3_REFERENCE_PUBLISHED"
    if not terminal:
        _atomic_json(status_path, failure)
    return attempt


def _write_content_addressed(root: pathlib.Path, stem: str,
                               payload: dict) -> tuple[pathlib.Path, str, int]:
    raw = _json_bytes(payload)
    digest = hashlib.sha256(raw).hexdigest()
    path = pathlib.Path(root) / f"{stem}-{digest}.json"
    if path.exists():
        existing = _read_regular(path, max(len(raw), 1), stem)
        if existing != raw:
            raise GateError("LOCAL_CONTENT_CONFLICT", str(path))
    else:
        _atomic_bytes(path, raw)
    return path, digest, len(raw)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _static_policy(path: pathlib.Path, expected_sha: str,
                   label: str) -> dict:
    payload, raw = _read_json(path, MAX_POLICY_EVIDENCE_BYTES, label)
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha:
        raise GateError(
            "STATIC_POLICY_HASH_MISMATCH",
            f"{label} {actual}, expected {expected_sha}",
        )
    return {
        "name": pathlib.Path(path).name,
        "sha256": actual,
        "size": len(raw),
        "json_root": "object" if isinstance(payload, dict) else "invalid",
    }


def _validate_operator_authorization(args) -> tuple[dict, bytes]:
    """Validate the immutable operator-applied policy authorization."""
    path = pathlib.Path(args.operator_authorization_file)
    authorization, raw = _read_json(
        path, MAX_POLICY_EVIDENCE_BYTES, "operator policy authorization")
    if (hashlib.sha256(raw).hexdigest() != OPERATOR_AUTHORIZATION_SHA256
            or authorization.get("schema_version")
            != "research-v3-operator-policy-authorization-v1"
            or authorization.get("state")
            != "OPERATOR_ATTESTED_APPLIED_AND_READ_BACK"
            or authorization.get("account_id") != "321572485933"
            or authorization.get("bucket") != DEFAULT_BUCKET
            or authorization.get("tagger_principal") != args.tagger_principal
            or authorization.get("tagger_identity_policy_sha256")
            != TAGGER_POLICY_SHA256
            or authorization.get("complete_bucket_policy_sha256")
            != BUCKET_POLICY_SHA256
            or authorization.get("evidence_tier")
            != "OPERATOR_ATTESTED_STATIC_POLICY_BASELINE"):
        raise GateError("OPERATOR_AUTHORIZATION_INVALID", str(path))
    return authorization, raw


def _tag_set(value: object, label: str, *,
             add_eligible: bool = False) -> list[dict[str, str]]:
    if not isinstance(value, dict) or not isinstance(value.get("TagSet"), list):
        raise GateError("POLICY_CANARY_INVALID", f"{label} TagSet missing")
    tags: dict[str, str] = {}
    for row in value["TagSet"]:
        if (not isinstance(row, dict) or not isinstance(row.get("Key"), str)
                or not isinstance(row.get("Value"), str)
                or row["Key"] in tags):
            raise GateError("POLICY_CANARY_INVALID", f"{label} tag malformed")
        tags[row["Key"]] = row["Value"]
    if add_eligible:
        tags["research-eligible"] = "true"
    if len(tags) > 10:
        raise GateError("POLICY_CANARY_INVALID", "S3 tag limit would be exceeded")
    return [{"Key": key, "Value": tags[key]} for key in sorted(tags)]


def _reusable_policy_patrol(root: pathlib.Path, date: str,
                            receipt_sha: str,
                            refresh_hours: int) -> pathlib.Path | None:
    pointer = pathlib.Path(root) / "CURRENT.json"
    if not pointer.exists():
        return None
    try:
        value, _raw = _read_json(pointer, MAX_AUDIT_BYTES,
                                 "policy patrol pointer")
        name = value.get("evidence_file")
        digest = value.get("evidence_sha256")
        if (value.get("schema_version")
                != "research-v3-policy-patrol-pointer-v1"
                or value.get("date") != date
                or value.get("receipt_set_sha256") != receipt_sha
                or not isinstance(name, str)
                or name != f"policy-patrol-evidence-{digest}.json"
                or not isinstance(digest, str)
                or SHA_RE.fullmatch(digest) is None):
            return None
        path = pathlib.Path(root) / name
        payload, raw = _read_json(
            path, MAX_POLICY_EVIDENCE_BYTES, "policy patrol evidence")
        generated = _parse_utc(
            payload.get("generated_at_utc"), "generated_at_utc")
        expected = {
            "tagger_identity_policy": TAGGER_POLICY_SHA256,
            "complete_bucket_policy": BUCKET_POLICY_SHA256,
            "publisher_inspection_delta": PUBLISHER_DELTA_SHA256,
        }
        if (hashlib.sha256(raw).hexdigest() != digest
                or payload.get("schema_version")
                != "research-v3-policy-patrol-evidence-v1"
                or payload.get("approved_static_policy_sha256") != expected
                or dt.datetime.now(dt.timezone.utc) - generated
                > dt.timedelta(hours=refresh_hours)):
            return None
        return path
    except GateError:
        return None


def refresh_policy_patrol_evidence(
        durable_index: pathlib.Path, date: str, args, *,
        tag_env: dict[str, str],
        publisher_environment: dict[str, str]) -> pathlib.Path:
    """Refresh static-hash plus live-behavior evidence without self-auditing.

    The principals available on the host cannot read IAM or bucket policy.
    Consequently this artifact deliberately does *not* claim global exclusive
    writer status and is never accepted as a canonical tagger audit.  An
    independent auditor must still produce the receipt-scoped
    ``SINGLE_WRITER_VERIFIED`` artifact.
    """
    index, receipt_path = _validate_durable_index(durable_index, date)
    receipt_sha = index["receipt_set_sha256"]
    root = (pathlib.Path(args.live_dir).absolute() / "research_v3_daily"
            / "policy-patrol" / f"date={date}" / f"receipt={receipt_sha}")
    reusable = _reusable_policy_patrol(
        root, date, receipt_sha, args.audit_refresh_hours)
    if reusable is not None:
        return reusable

    # No tag mutation occurs until the local seal is reverified, the durable
    # operator authorization is validated, and the exact remote authority
    # receipt is byte-for-byte reproduced by its non-null VersionId.
    _verify_seal(date, args)
    _validate_operator_authorization(args)
    policies = {
        "tagger_identity_policy": _static_policy(
            pathlib.Path(args.tagger_policy_file), TAGGER_POLICY_SHA256,
            "tagger identity policy"),
        "complete_bucket_policy": _static_policy(
            pathlib.Path(args.bucket_policy_file), BUCKET_POLICY_SHA256,
            "complete bucket policy"),
        "publisher_inspection_delta": _static_policy(
            pathlib.Path(args.publisher_delta_file), PUBLISHER_DELTA_SHA256,
            "publisher inspection delta"),
    }
    aws = os.path.abspath(args.aws_cli)
    _require_arms(args)
    tagger_identity = _identity(
        [aws, "sts", "get-caller-identity", "--output", "json"],
        args.tagger_principal, cwd=ROOT, env=tag_env,
        label="policy canary tagger caller")
    binding = index["receipt_object"]
    if (not _valid_version_id(binding.get("VersionId"))
            or not isinstance(binding.get("size"), int)
            or binding["size"] <= 0
            or binding["size"] > MAX_RECEIPT_BYTES):
        raise GateError("POLICY_CANARY_INVALID", "authority receipt is not bounded")
    head = _json_document(_publisher_run([
        aws, "s3api", "head-object", "--bucket", DEFAULT_BUCKET,
        "--key", binding["key"], "--version-id", binding["VersionId"],
        "--output", "json",
    ], args=args, publisher_environment=publisher_environment,
        label="policy canary exact authority preflight", timeout=120),
        "policy canary exact authority preflight")
    if (head.get("VersionId") != binding["VersionId"]
            or head.get("ContentLength") != binding["size"]
            or head.get("ContentLength") > MAX_RECEIPT_BYTES):
        raise GateError(
            "POLICY_CANARY_INVALID", "receipt exact HEAD binding mismatch")
    with tempfile.TemporaryDirectory(
            prefix="research-v3-authority-") as temp_value:
        remote_path = pathlib.Path(temp_value) / "receipt.json"
        observed = _exact_remote_catalog_bytes(
            binding["key"], binding["VersionId"], remote_path, args=args,
            publisher_environment=publisher_environment)
        if observed != (binding["size"], binding["sha256"]):
            raise GateError(
                "POLICY_CANARY_INVALID", "remote authority receipt mismatch")
        remote_receipt, _remote_raw = _read_json(
            remote_path, MAX_RECEIPT_BYTES, "exact remote authority receipt")
    _require_rfq_off(remote_receipt, "exact remote authority receipt")
    tools_dir = str(ROOT / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    try:
        import canonical_eligibility_tagger as eligibility
        selected = eligibility.select_tag_targets(
            remote_receipt, DEFAULT_PREFIX)
    except Exception as exc:
        raise GateError("POLICY_CANARY_INVALID", str(exc)) from exc
    candidates = sorted([
        obj for obj in selected
        if (obj.get("bucket") == DEFAULT_BUCKET
            and _valid_version_id(obj.get("VersionId"))
            and isinstance(obj.get("size"), int)
            and 0 < obj["size"] <= MAX_PATROL_CANARY_BYTES
            and isinstance(obj.get("sha256"), str)
            and SHA_RE.fullmatch(obj["sha256"]) is not None)
    ], key=lambda obj: (obj["size"], obj["key"], obj["VersionId"]))
    if not candidates:
        raise GateError(
            "POLICY_CANARY_INVALID",
            "no bounded non-RFQ research candidate exists",
        )
    target = candidates[0]
    target_head = _json_document(_publisher_run([
        aws, "s3api", "head-object", "--bucket", DEFAULT_BUCKET,
        "--key", target["key"], "--version-id", target["VersionId"],
        "--output", "json",
    ], args=args, publisher_environment=publisher_environment,
        label="policy canary exact target preflight", timeout=120),
        "policy canary exact target preflight")
    if (target_head.get("VersionId") != target["VersionId"]
            or target_head.get("ContentLength") != target["size"]
            or target_head.get("ContentLength") > MAX_PATROL_CANARY_BYTES):
        raise GateError(
            "POLICY_CANARY_INVALID", "candidate exact HEAD binding mismatch")
    key = target["key"]
    version_id = target["VersionId"]
    with tempfile.TemporaryDirectory(prefix="research-v3-canary-") as temp:
        exact = pathlib.Path(temp) / "exact-version.bin"
        observed = _exact_remote_catalog_bytes(
            key, version_id, exact, args=args,
            publisher_environment=publisher_environment)
        if observed != (target["size"], target["sha256"]):
            raise GateError(
                "POLICY_CANARY_INVALID", "exact candidate bytes do not match receipt")

    before = _json_document(_run(tagger_command(
        aws, args.tagger_principal, [
        aws, "s3api", "get-object-tagging", "--bucket", DEFAULT_BUCKET,
        "--key", key, "--version-id", version_id, "--output", "json",
    ]), cwd=ROOT, env=tag_env, label="tagger exact tag preflight",
        timeout=120), "tagger exact tag preflight")
    desired = _tag_set(
        before, "tagger exact tag preflight", add_eligible=True)
    tagging = json.dumps({"TagSet": desired}, sort_keys=True,
                         separators=(",", ":"))
    _require_arms(args)
    _run(tagger_command(aws, args.tagger_principal, [
        aws, "s3api", "put-object-tagging", "--bucket", DEFAULT_BUCKET,
        "--key", key, "--version-id", version_id, "--tagging", tagging,
    ]), cwd=ROOT, env=tag_env, label="tagger exact tag positive canary",
        timeout=120)
    after = _json_document(_run(tagger_command(
        aws, args.tagger_principal, [
        aws, "s3api", "get-object-tagging", "--bucket", DEFAULT_BUCKET,
        "--key", key, "--version-id", version_id, "--output", "json",
    ]), cwd=ROOT, env=tag_env, label="tagger exact tag readback",
        timeout=120), "tagger exact tag readback")
    if _tag_set(after, "tagger exact tag readback") != desired:
        raise GateError("POLICY_CANARY_INVALID", "tagger tag readback mismatch")

    _require_arms(args)
    _run_expect_denied(tagger_command(aws, args.tagger_principal, [
        aws, "s3api", "put-object-tagging", "--bucket", DEFAULT_BUCKET,
        "--key", key, "--tagging", tagging,
    ]), cwd=ROOT, env=tag_env,
        label="tagger versionless tag negative canary")

    _require_arms(args)
    _run_expect_denied(
        publisher_command(aws, args.publisher_principal, [
            aws, "s3api", "put-object-tagging", "--bucket", DEFAULT_BUCKET,
            "--key", key, "--version-id", version_id,
            "--tagging", tagging,
        ]), cwd=ROOT, env=publisher_environment,
        label="publisher exact tag write negative canary")

    audited_at = _utc_now()
    approved_hashes = {
        "tagger_identity_policy": TAGGER_POLICY_SHA256,
        "complete_bucket_policy": BUCKET_POLICY_SHA256,
        "publisher_inspection_delta": PUBLISHER_DELTA_SHA256,
    }
    evidence_payload = {
        "schema_version": "research-v3-policy-patrol-evidence-v1",
        "state": "BEHAVIORAL_CANARY_PASS_STATIC_POLICY_BYTES_MATCH",
        "generated_at_utc": audited_at,
        "bucket": DEFAULT_BUCKET,
        "date": date,
        "receipt_set_sha256": receipt_sha,
        "evidence_model": "STATIC_REPOSITORY_HASH_PLUS_LIVE_BEHAVIOR",
        "live_policy_readback": "UNAVAILABLE_TO_PRODUCTION_IDENTITIES",
        "global_single_writer_proven": False,
        "not_a_tagger_authorization_audit": True,
        "limitations": [
            "does not read the live bucket policy",
            "does not enumerate every IAM principal or policy attachment",
            "does not prove exclusive_exact_version_writer",
            "requires an independent receipt-scoped live-policy audit",
        ],
        "approved_static_policy_sha256": approved_hashes,
        "static_policy_artifacts": policies,
        "tagger_identity": tagger_identity,
        "publisher_identity": {"Arn": args.publisher_principal,
                               "verified_in_same_exec_shell": True},
        "canary_target": {"bucket": DEFAULT_BUCKET, "key": key,
                          "VersionId": version_id,
                          "class": "SMALLEST_BOUNDED_RECEIPT_CANDIDATE"},
        "checks": {
            "tagger_exact_version_get": "PASS",
            "tagger_exact_version_put_preserve_and_readback": "PASS",
            "tagger_versionless_put": "ACCESS_DENIED",
            "publisher_exact_version_get_tagging": "UNAVAILABLE_EXPECTED",
            "publisher_exact_version_put_tagging": "ACCESS_DENIED",
            "tag_enforcement":
                "TAGGER_READBACK_PLUS_W09_CONSUMER_REVALIDATION",
            "rfq": "OFF",
        },
    }
    evidence_path, evidence_sha, _evidence_size = _write_content_addressed(
        root, "policy-patrol-evidence", evidence_payload)
    pointer = {
        "schema_version": "research-v3-policy-patrol-pointer-v1",
        "date": date,
        "receipt_set_sha256": receipt_sha,
        "evidence_file": evidence_path.name,
        "evidence_sha256": evidence_sha,
        "updated_at_utc": audited_at,
    }
    _atomic_json(root / "CURRENT.json", pointer)
    return evidence_path


def refresh_layered_single_writer_audit(
        durable_index: pathlib.Path, date: str, args, *,
        patrol_evidence: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """Bind durable operator authorization to a fresh behavioral patrol.

    This is an explicit transition tier: it preserves the operator's applied
    policy authorization and refreshes tested-path liveness automatically,
    while recording that administrative drift is not live-enumerated.
    """
    index, _receipt_path = _validate_durable_index(durable_index, date)
    receipt_sha = index["receipt_set_sha256"]
    audit_root = pathlib.Path(args.audit_root).absolute()
    current_audit, current_evidence = _audit_pair(
        audit_root, date, receipt_sha)
    if current_audit.exists() and current_evidence.exists():
        try:
            _validate_audit(
                current_audit, current_evidence, date, receipt_sha,
                args.tagger_principal, require_fresh=True)
            current, _raw = _read_json(
                current_audit, MAX_AUDIT_BYTES, "current layered audit")
            audited = _parse_utc(current.get("audited_at_utc"), "audited_at_utc")
            if (current.get("evidence_tier")
                    == "OPERATOR_AUTHORIZATION_PLUS_BEHAVIORAL_PATROL"
                    and dt.datetime.now(dt.timezone.utc) - audited
                    <= dt.timedelta(hours=args.audit_refresh_hours)):
                return current_audit, current_evidence
        except GateError:
            pass

    pending_root = (pathlib.Path(args.live_dir).absolute()
                    / "canonical_receipts" / "tagged" / f"date={date}")
    if (any(pending_root.glob("TAGGED-PENDING-*.json"))
            and not any(pending_root.glob("TAGGED-DURABLE-*.json"))):
        raise GateError(
            "TAGGER_RECOVERY_REQUIRES_HISTORICAL_AUDIT",
            "a pending tag transaction exists but its audit is no longer "
            "fresh; rotating the audit would conflict with remote receipt bytes",
        )

    authorization_path = pathlib.Path(args.operator_authorization_file)
    _authorization, authorization_raw = _validate_operator_authorization(args)
    patrol, patrol_raw = _read_json(
        patrol_evidence, MAX_POLICY_EVIDENCE_BYTES,
        "behavioral policy patrol")
    patrol_sha = hashlib.sha256(patrol_raw).hexdigest()
    generated = _parse_utc(
        patrol.get("generated_at_utc"), "patrol generated_at_utc")
    if (patrol.get("schema_version") != "research-v3-policy-patrol-evidence-v1"
            or patrol.get("state")
            != "BEHAVIORAL_CANARY_PASS_STATIC_POLICY_BYTES_MATCH"
            or patrol.get("date") != date
            or patrol.get("receipt_set_sha256") != receipt_sha
            or patrol.get("global_single_writer_proven") is not False
            or dt.datetime.now(dt.timezone.utc) - generated
            > dt.timedelta(hours=24)):
        raise GateError("POLICY_CANARY_INVALID", str(patrol_evidence))

    limitations = [
        "operator policy hashes are a durable authorization, not live readback",
        "daily patrol covers tagger and publisher tested paths, not every principal",
        "independent administrative drift enumeration remains engineering debt",
    ]
    combined = {
        "schema_version": "canonical-eligibility-layered-policy-evidence-v1",
        "state": "OPERATOR_AUTHORIZATION_AND_FRESH_PATROL_BOUND",
        "date": date,
        "bucket": DEFAULT_BUCKET,
        "receipt_set_sha256": receipt_sha,
        "evidence_tier": "OPERATOR_AUTHORIZATION_PLUS_BEHAVIORAL_PATROL",
        "operator_authorization": {
            "path_name": authorization_path.name,
            "size": len(authorization_raw),
            "sha256": OPERATOR_AUTHORIZATION_SHA256,
        },
        "behavioral_patrol": {
            "path_name": pathlib.Path(patrol_evidence).name,
            "size": len(patrol_raw),
            "sha256": patrol_sha,
            "generated_at_utc": patrol["generated_at_utc"],
        },
        "live_policy_readback": False,
        "limitations": limitations,
        "rfq": "OFF",
    }
    root = (audit_root / f"date={date}" / f"receipt={receipt_sha}")
    evidence_path, evidence_sha, evidence_size = _write_content_addressed(
        root, "policy-evidence", combined)
    audited_at = _utc_now()
    audit_payload = {
        "schema_version": "canonical-eligibility-single-writer-audit-v1",
        "state": "SINGLE_WRITER_VERIFIED",
        "date": date,
        "bucket": DEFAULT_BUCKET,
        "receipt_set_sha256": receipt_sha,
        "tag_key": "research-eligible",
        "dedicated_tagger_principal": args.tagger_principal,
        "dedicated_tagger_sts_caller_arn": args.tagger_principal,
        "exclusive_exact_version_writer": True,
        "versionless_tagging_denied": True,
        "other_automation_tag_writers_denied": True,
        "evidence_tier": "OPERATOR_AUTHORIZATION_PLUS_BEHAVIORAL_PATROL",
        "operator_authorization_sha256": OPERATOR_AUTHORIZATION_SHA256,
        "behavioral_patrol_sha256": patrol_sha,
        "live_policy_readback": False,
        "limitations": limitations,
        "audited_at_utc": audited_at,
        "auditor": "layered-authorization-patrol-v1",
        "policy_evidence_size": evidence_size,
        "policy_evidence_sha256": evidence_sha,
    }
    audit_path, audit_sha, _audit_size = _write_content_addressed(
        root, "single-writer-audit", audit_payload)
    _atomic_json(root / "CURRENT.json", {
        "schema_version": "research-v3-current-policy-audit-pointer-v1",
        "date": date,
        "receipt_set_sha256": receipt_sha,
        "audit_file": audit_path.name,
        "audit_sha256": audit_sha,
        "policy_evidence_file": evidence_path.name,
        "policy_evidence_sha256": evidence_sha,
        "updated_at_utc": audited_at,
    })
    _validate_audit(
        audit_path, evidence_path, date, receipt_sha, args.tagger_principal,
        require_fresh=True, expected_sha256=audit_sha)
    return audit_path, evidence_path


def _print_plan(plan: DatePlan, *, mode: str, live_dir: pathlib.Path,
                dest: str) -> None:
    print(json.dumps({
        "state": "V3_DAILY_PLAN",
        "mode": mode,
        "date": plan.date,
        "durable_index": str(plan.durable_index),
        "durable_receipt_set_sha256": plan.durable_set_sha256,
        "tagged_index": str(plan.tagged_index) if plan.tagged_index else None,
        "tagger_required": plan.tagged_index is None,
        "publisher_required": True,
        "publisher_live_dir": str(pathlib.Path(live_dir).absolute()),
        "destination": dest,
        "rfq": "OFF",
        "aws_mutations": 0 if mode in ("CHECK", "DRY_RUN") else "GATED",
    }, sort_keys=True))


def execute_date(plan: DatePlan, args, *, tag_env: dict[str, str],
                 publisher_environment: dict[str, str]):
    python = os.path.abspath(args.python)
    aws = os.path.abspath(args.aws_cli) if "/" in args.aws_cli else args.aws_cli

    tagged = plan.tagged_index
    if tagged is None:
        pending_root = (pathlib.Path(args.live_dir).absolute()
                        / "canonical_receipts" / "tagged"
                        / f"date={plan.date}")
        pending = sorted(pending_root.glob("TAGGED-PENDING-*.json"))
        if len(pending) > 1:
            raise GateError("TAGGER_RECOVERY_AMBIGUOUS", str(pending_root))
        if pending:
            intent, _raw = _read_json(
                pending[0], MAX_INDEX_BYTES, "tagged pending intent")
            old_audit = intent.get("eligibility_single_writer_audit_sha256")
            if old_audit != plan.audit_sha256:
                raise GateError(
                    "TAGGER_RECOVERY_REQUIRES_HISTORICAL_AUDIT",
                    f"pending audit {old_audit!r} differs from current "
                    f"{plan.audit_sha256}; do not create conflicting receipt bytes",
                )
        _require_arms(args)
        _verify_seal(plan.date, args)
        _identity(
            [aws, "sts", "get-caller-identity", "--output", "json"],
            args.tagger_principal, cwd=ROOT, env=tag_env,
            label="tagger caller",
        )
        _require_arms(args)
        tag_command = [
            python, str(ROOT / "tools" / "canonical_eligibility_tagger.py"),
            "--receipt-index", str(plan.durable_index),
            "--single-writer-audit", str(plan.audit),
            "--policy-evidence", str(plan.policy_evidence),
            "--bucket", DEFAULT_BUCKET,
            "--prefix", DEFAULT_PREFIX,
            "--aws-cli", aws,
            "--operator-approved",
            "--output-root", str(pathlib.Path(args.live_dir).absolute()
                                  / "canonical_receipts" / "tagged"),
        ]
        if plan.historical_existing_only:
            if plan.pending_index is None:
                raise GateError(
                    "TAGGER_RECOVERY_INVALID",
                    "historical recovery has no bound pending marker")
            tag_command.extend([
                "--recover-existing-only",
                "--pending-index", str(plan.pending_index),
            ])
        output = _run(
            tag_command, cwd=ROOT, env=tag_env,
            label=(f"historical existing-only tag recovery {plan.date}"
                   if plan.historical_existing_only
                   else f"eligibility tagging {plan.date}"))
        try:
            result = json.loads(output.strip().splitlines()[-1])
            tagged = pathlib.Path(result["index"]).absolute()
            if plan.historical_existing_only and (
                    result.get("recovery")
                    != "HISTORICAL_EXISTING_REMOTE_ONLY"
                    or result.get("receipt_puts") != 0
                    or result.get("rfq") != "OFF"):
                raise ValueError(
                    "historical recovery did not prove zero remote receipt creates")
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise GateError("TAGGER_OUTPUT_INVALID", str(exc)) from exc
        _validate_tagged_index(tagged, plan.date, plan.durable_set_sha256)

    yellow = (pathlib.Path(args.live_dir).absolute()
              / "research_reference_patrol" / "YELLOW.json")
    if os.path.lexists(yellow):
        raise GateError("REFERENCE_PATROL_YELLOW", str(yellow))

    # Expensive schema/fact/catalog reconstruction happens before the short
    # proof window and emits only a local, content-addressed plan (zero S3
    # writes).  The later publisher commit reopens only the small immutable
    # control receipts, never the facts/catalog/dim set.
    _verify_seal(plan.date, args)
    prepared_root = (
        pathlib.Path(args.live_dir).absolute() / "research_v3_daily"
        / f"date={plan.date}" / f"receipt={plan.durable_set_sha256}"
        / "prepared")
    prepared_root.mkdir(parents=True, exist_ok=True, mode=0o750)
    prepare = [
        python, str(ROOT / "tools" / "research_release.py"),
        "publish-reference", "--date", plan.date,
        "--receipt", str(tagged),
        "--prepare-only", "--prepare-output-root", str(prepared_root),
        "--dest", args.dest,
        "--no-rfq",
        "--quality-dir", str(pathlib.Path(args.quality_dir).absolute()),
        "--live-dir", str(pathlib.Path(args.live_dir).absolute()),
        "--raw-vault", args.raw_vault,
        "--operator-approved",
    ]
    prepare_output = _publisher_run(
        prepare, args=args, publisher_environment=publisher_environment,
        label=f"v3 reference prepare {plan.date}")
    prepared_plan, prepared_result = _validate_prepared_result(
        prepare_output, date=plan.date, prepared_root=prepared_root)

    # The dedicated tagger (never the publisher) now re-reads the tagged
    # receipt and every non-RFQ candidate exact VersionId.  The publisher gets
    # only this local proof, so tagger credentials never cross identities.
    proof_output = _run([
        python, str(ROOT / "tools" / "canonical_eligibility_tagger.py"),
        "--verify-only",
        "--tagged-index", str(tagged),
        "--byte-receipt-index", str(plan.durable_index),
        "--expected-tagger-principal", args.tagger_principal,
        "--bucket", DEFAULT_BUCKET,
        "--prefix", DEFAULT_PREFIX,
        "--aws-cli", aws,
        "--proof-output-root", str(
            pathlib.Path(args.live_dir).absolute()
            / "canonical_receipts" / "tag-precommit"),
    ], cwd=ROOT, env=tag_env,
        label=f"precommit exact tag proof {plan.date}")
    try:
        proof_result = json.loads(proof_output.strip().splitlines()[-1])
        proof = pathlib.Path(proof_result["proof"]).absolute()
        if (proof_result.get("state")
                != "EXACT_VERSION_TAG_READBACK_VERIFIED"
                or proof_result.get("tag_puts") != 0
                or proof_result.get("rfq") != "OFF"):
            raise ValueError("verify-only result is not a zero-PUT RFQ-off proof")
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise GateError("TAG_PRECOMMIT_PROOF_INVALID", str(exc)) from exc

    commit = [
        python, str(ROOT / "tools" / "research_release.py"),
        "publish-reference", "--date", plan.date,
        "--receipt", str(tagged),
        "--prepared-plan", str(prepared_plan),
        "--tag-precommit-proof", str(proof),
        "--dest", args.dest,
        "--no-rfq",
        "--quality-dir", str(pathlib.Path(args.quality_dir).absolute()),
        "--live-dir", str(pathlib.Path(args.live_dir).absolute()),
        "--raw-vault", args.raw_vault,
        "--operator-approved",
    ]
    commit_output = _publisher_run(
        commit, args=args, publisher_environment=publisher_environment,
        label=f"v3 reference manifest commit {plan.date}")
    commit_result = _validate_manifest_commit_result(
        commit_output, date=plan.date,
        prepared_plan_sha256=prepared_result["prepared_plan_sha256"])
    if commit_result["release_id"] != prepared_result["release_id"]:
        raise GateError(
            "REFERENCE_COMMIT_OUTPUT_INVALID",
            "commit release_id differs from the prepared plan",
        )
    return tagged, commit_result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", action="append", default=[])
    parser.add_argument("--queue", type=pathlib.Path,
                        help="bounded text file containing one YYYY-MM-DD per line")
    parser.add_argument("--yesterday", action="store_true",
                        help="enqueue the previous UTC date")
    parser.add_argument(
        "--scan-ready-days", type=int, default=0,
        help="discover sealed UTC dates in a bounded lookback and retry them")
    parser.add_argument("--live-dir", required=True,
                        help="explicit main production work/live directory")
    parser.add_argument(
        "--receipt-root",
        help="read-only canonical_receipts root for detached historical receipts")
    parser.add_argument("--audit-root", required=True,
                        help="date=D directories containing audit/evidence JSON")
    parser.add_argument("--raw-root", required=True)
    parser.add_argument("--warehouse-root", required=True)
    parser.add_argument(
        "--tagger-credentials-file",
        default=str(DEFAULT_TAGGER_CREDENTIAL_FILE))
    parser.add_argument("--tagger-profile",
                        default=DEFAULT_TAGGER_PROFILE)
    parser.add_argument("--publisher-env-file", required=True)
    parser.add_argument("--tagger-principal", default=DEFAULT_TAGGER_ARN)
    parser.add_argument("--publisher-principal", default=DEFAULT_PUBLISHER_ARN)
    parser.add_argument("--home", default=str(pathlib.Path.home()))
    parser.add_argument("--region", default="us-east-2")
    parser.add_argument("--dest", default=DEFAULT_DEST)
    parser.add_argument("--raw-vault",
                        default="s3://kalshi-vault-ritcardo/ec2/raw")
    parser.add_argument("--quality-dir",
                        default=str(DEFAULT_PRODUCTION_QUALITY_DIR))
    parser.add_argument(
        "--cutover-arm-file", default=str(DEFAULT_CUTOVER_ARM))
    parser.add_argument(
        "--publish-arm-file", default=str(DEFAULT_PUBLISH_ARM))
    parser.add_argument("--tagger-policy-file", default=str(DEFAULT_TAGGER_POLICY))
    parser.add_argument("--bucket-policy-file", default=str(DEFAULT_BUCKET_POLICY))
    parser.add_argument(
        "--publisher-delta-file", default=str(DEFAULT_PUBLISHER_DELTA))
    parser.add_argument(
        "--operator-authorization-file",
        default=str(DEFAULT_OPERATOR_AUTHORIZATION))
    parser.add_argument(
        "--audit-refresh-hours", type=int, default=12,
        help="reuse a passing live policy canary for at most this many hours")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--aws-cli", default="aws")
    parser.add_argument("--operator-approved", action="store_true")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true",
                       help="validate local gates and print plans; no AWS calls")
    modes.add_argument("--dry-run", action="store_true",
                       help="render the complete per-date plan; no AWS calls")
    modes.add_argument(
        "--durable-only", action="store_true",
        help=("production mode: verify seal+witness, sync controls, and "
              "publish the full exact-version durable receipt; never load "
              "tagger credentials, tag objects, or publish research manifests"))
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        mode = ("CHECK" if args.check else "DRY_RUN" if args.dry_run
                else "DURABLE_ONLY" if args.durable_only else "RUN")
        live_arg = pathlib.Path(args.live_dir)
        raw_arg = pathlib.Path(args.raw_root)
        warehouse_arg = pathlib.Path(args.warehouse_root)
        quality_arg = pathlib.Path(args.quality_dir)
        if any(not path.is_absolute() for path in (
                live_arg, raw_arg, warehouse_arg, quality_arg)):
            raise GateError(
                "PRODUCTION_PATH_INVALID", "all production roots must be absolute")
        live_dir = live_arg.absolute()
        receipt_root = (pathlib.Path(args.receipt_root).absolute()
                        if args.receipt_root else None)
        discovery_failures: list[dict] = []
        dates = load_dates(
            args.date, args.queue, args.yesterday,
            scan_ready_days=args.scan_ready_days,
            warehouse_root=warehouse_arg,
            discovery_failures=discovery_failures)
        dates = sorted(set(dates) | set(discover_unfinished_dates(
            live_arg, discovery_failures)))
        authorized_dates = []
        for date in dates:
            if _date_scope_allowed(date):
                authorized_dates.append(date)
            else:
                _record_date_scope_block(
                    discovery_failures, date, "final execution gate")
        dates = authorized_dates

        if args.check or args.dry_run:
            failures = len(discovery_failures)
            for issue in discovery_failures:
                print(json.dumps({
                    "state": "V3_DAILY_DISCOVERY_BLOCKED",
                    "mode": mode,
                    "date": issue["date"],
                    "code": issue["code"],
                    "detail": issue["detail"],
                    "rfq": "OFF",
                    "aws_mutations": 0,
                }, sort_keys=True), file=sys.stderr)
            for date in dates:
                try:
                    durable = _find_durable(date, live_dir, receipt_root)
                    if durable is None:
                        print(json.dumps({
                            "state": "V3_DAILY_PLAN",
                            "mode": mode,
                            "date": date,
                            "durable_required": True,
                            "chain": [
                                "verify-seal", "freeze-forward",
                                "sync-controls", "metadata-preflight",
                                "publish-receipt-full-content-verification",
                                "eligibility-audit", "eligibility-tags",
                                "publish-reference",
                            ],
                            "rfq": "OFF",
                            "aws_mutations": 0,
                        }, sort_keys=True))
                        continue
                    plan = prepare_date_plan(
                        date, live_dir, pathlib.Path(args.audit_root),
                        args.tagger_principal, receipt_root)
                    _print_plan(
                        plan, mode=mode, live_dir=live_dir, dest=args.dest)
                except GateError as exc:
                    failures += 1
                    print(json.dumps({
                        "state": "V3_DAILY_PLAN_BLOCKED",
                        "mode": mode,
                        "date": date,
                        "code": exc.code,
                        "detail": exc.detail,
                        "rfq": "OFF",
                        "aws_mutations": 0,
                    }, sort_keys=True), file=sys.stderr)
            return 2 if failures else 0

        if not args.operator_approved:
            raise GateError(
                "OPERATOR_GATE",
                "--operator-approved is required for authorized AWS writes",
            )
        if args.dest != DEFAULT_DEST:
            raise GateError("DESTINATION_INVALID", "production destination is fixed")
        if not args.durable_only:
            if args.tagger_principal != DEFAULT_TAGGER_ARN:
                raise GateError(
                    "TAGGER_PRINCIPAL_INVALID", args.tagger_principal)
            if args.tagger_profile != DEFAULT_TAGGER_PROFILE:
                raise GateError("TAGGER_PROFILE_INVALID", args.tagger_profile)
        if args.publisher_principal != DEFAULT_PUBLISHER_ARN:
            raise GateError("PUBLISHER_PRINCIPAL_INVALID", args.publisher_principal)
        live_dir = _exact_existing_dir(
            live_arg, DEFAULT_PRODUCTION_LIVE_DIR, "production live root")
        _exact_existing_dir(raw_arg, DEFAULT_PRODUCTION_RAW_ROOT, "raw root")
        _exact_existing_dir(
            warehouse_arg, DEFAULT_PRODUCTION_WAREHOUSE_ROOT, "warehouse root")
        _exact_existing_dir(
            quality_arg, DEFAULT_PRODUCTION_QUALITY_DIR, "quality root")
        home = _exact_existing_dir(
            pathlib.Path(args.home), DEFAULT_PRODUCTION_HOME, "HOME")
        audit_root = pathlib.Path(args.audit_root)
        if (not audit_root.is_absolute()
                or audit_root != live_dir / "research_v3_audit"):
            raise GateError(
                "PRODUCTION_PATH_INVALID",
                "audit root must be production live/research_v3_audit",
            )
        expected_publisher_file = (
            DEFAULT_DURABLE_PUBLISHER_ENV_FILE
            if args.durable_only else DEFAULT_PUBLISHER_ENV_FILE)
        if pathlib.Path(args.publisher_env_file) != expected_publisher_file:
            raise GateError(
                "CREDENTIAL_FILE_INVALID",
                "publisher credential must come from the systemd credential mount",
            )
        if (not args.durable_only
                and pathlib.Path(args.tagger_credentials_file)
                != DEFAULT_TAGGER_CREDENTIAL_FILE):
            raise GateError(
                "CREDENTIAL_FILE_INVALID",
                "tagger credential must come from the systemd credential mount",
            )
        if pathlib.Path(args.cutover_arm_file) != DEFAULT_CUTOVER_ARM:
            raise GateError("OPERATOR_GATE", "cutover arm path is fixed")
        if pathlib.Path(args.publish_arm_file) != DEFAULT_PUBLISH_ARM:
            raise GateError("OPERATOR_GATE", "publish arm path is fixed")
        if not pathlib.Path(args.aws_cli).is_absolute():
            raise GateError("AWS_BINARY_INVALID", "--aws-cli must be absolute")
        if not pathlib.Path(args.python).is_absolute():
            raise GateError("PYTHON_BINARY_INVALID", "--python must be absolute")
        _require_arms(args)
        publisher_file = _secure_secret_file(
            pathlib.Path(args.publisher_env_file), "publisher environment file")
        tag_env = None
        if not args.durable_only:
            tagger_credential = _secure_secret_file(
                pathlib.Path(args.tagger_credentials_file),
                "tagger credential file")
            tag_env = tagger_env(
                home, tagger_credential,
                args.tagger_profile, args.region)
        publisher_environment = publisher_env(home, publisher_file, args.region)
        publisher_environment.update({
            "RAW_ROOT": str(raw_arg),
            "WAREHOUSE_ROOT": str(warehouse_arg),
            "ARCHIVE_ROOT": str(warehouse_arg / "facts"),
            "RESEARCH_STAGE_ROOT":
                "/home/ubuntu/hft-bot/work/research_stage",
        })

        state_root = live_dir / "research_v3_daily"
        state_root.mkdir(parents=True, exist_ok=True, mode=0o750)
        lock_path = state_root / "orchestrator.lock"
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise GateError("ORCHESTRATOR_BUSY", str(lock_path)) from exc
            failures = []
            for issue in discovery_failures:
                failure = {
                    "schema_version": "research-v3-daily-status-v1",
                    "state": "BLOCKED",
                    "date": issue["date"],
                    "code": issue["code"],
                    "detail": issue["detail"],
                    "rfq": "OFF",
                    "failed_at_utc": _utc_now(),
                }
                discovery_status = (state_root / f"date={issue['date']}"
                                    / "DISCOVERY_STATUS.json")
                _record_attempt_failure(discovery_status, failure)
                failures.append(failure)
                print(json.dumps(failure, sort_keys=True), file=sys.stderr)
            if args.durable_only:
                for date in dates:
                    status_path = (state_root / f"date={date}"
                                   / "DURABLE_STATUS.json")
                    try:
                        durable_path = ensure_durable(
                            date, args,
                            publisher_environment=publisher_environment)
                        durable_index, receipt_path = _validate_durable_index(
                            durable_path, date)
                        status = {
                            "schema_version":
                                "research-v3-durable-only-status-v1",
                            "state": "DURABLE_RECEIPT_READY",
                            "date": date,
                            "durable_index": str(durable_path),
                            "durable_receipt": str(receipt_path),
                            "receipt_set_sha256":
                                durable_index["receipt_set_sha256"],
                            "publisher_principal": args.publisher_principal,
                            "credential_mode": "PUBLISHER_ENV_ONLY",
                            "tagger_credentials_loaded": False,
                            "tag_writes": 0,
                            "research_manifest_writes": 0,
                            "rfq": "OFF",
                            "completed_at_utc": _utc_now(),
                        }
                        _atomic_json(status_path, status)
                        print(json.dumps(status, sort_keys=True))
                    except GateError as exc:
                        failure = {
                            "schema_version":
                                "research-v3-durable-only-status-v1",
                            "state": "BLOCKED",
                            "date": date,
                            "code": exc.code,
                            "detail": exc.detail,
                            "tagger_credentials_loaded": False,
                            "tag_writes": 0,
                            "research_manifest_writes": 0,
                            "rfq": "OFF",
                            "failed_at_utc": _utc_now(),
                        }
                        _record_attempt_failure(status_path, failure)
                        failures.append(failure)
                        print(json.dumps(failure, sort_keys=True),
                              file=sys.stderr)
                return 2 if failures else 0
            patrol_candidates: list[tuple[str, pathlib.Path]] = []
            for date in dates:
                status_path = state_root / f"date={date}" / "PREPARE_STATUS.json"
                try:
                    durable_path = ensure_durable(
                        date, args,
                        publisher_environment=publisher_environment)
                    durable_index, _receipt_path = _validate_durable_index(
                        durable_path, date)
                    receipt_sha = durable_index["receipt_set_sha256"]
                    status_path = (state_root / f"date={date}"
                                   / f"receipt={receipt_sha}"
                                   / "STATUS.json")
                    date_state_root = state_root / f"date={date}"
                    for prior_status in date_state_root.glob(
                            "receipt=*/STATUS.json"):
                        if prior_status == status_path:
                            continue
                        prior, _raw = _read_json(
                            prior_status, MAX_AUDIT_BYTES, "prior daily status")
                        if prior.get("state") == "V3_REFERENCE_PUBLISHED":
                            if (prior.get("schema_version")
                                    != TERMINAL_STATUS_SCHEMA):
                                raise GateError(
                                    "TERMINAL_STATE_UNMIGRATED",
                                    f"{prior_status} predates exact MANIFEST "
                                    "terminal binding; reconcile before a "
                                    "different receipt can publish",
                                )
                            prior_match = re.fullmatch(
                                r"receipt=([0-9a-f]{64})",
                                prior_status.parent.name)
                            if prior_match is None:
                                raise GateError(
                                    "TERMINAL_STATE_INVALID",
                                    str(prior_status))
                            _verify_terminal_status(
                                prior, prior_status, date,
                                prior_match.group(1), live_dir, audit_root,
                                args.tagger_principal, args=args,
                                publisher_environment=publisher_environment)
                            raise GateError(
                                "IMMUTABLE_DATE_RECEIPT_CHANGED",
                                f"{date} already published receipt "
                                f"{prior.get('durable_receipt_set_sha256')}; "
                                f"new receipt is {receipt_sha}",
                            )
                    if status_path.exists():
                        old, _raw = _read_json(
                            status_path, MAX_AUDIT_BYTES, "daily status")
                        if (old.get("state") == "V3_REFERENCE_PUBLISHED"
                                and old.get("durable_receipt_set_sha256")
                                == receipt_sha):
                            if (old.get("schema_version")
                                    == TERMINAL_STATUS_SCHEMA):
                                _tagged, manifest_binding = \
                                    _verify_terminal_status(
                                        old, status_path, date, receipt_sha,
                                        live_dir, audit_root,
                                        args.tagger_principal, args=args,
                                        publisher_environment=
                                        publisher_environment)
                                print(json.dumps({
                                    "state":
                                        "V3_REFERENCE_ALREADY_PUBLISHED",
                                    "date": date,
                                    "durable_receipt_set_sha256": receipt_sha,
                                    "release_id": old["release_id"],
                                    "manifest_object": manifest_binding,
                                    "terminal_verification":
                                        "EXACT_VERSION_FULL_SHA256",
                                    "rfq": "OFF",
                                }, sort_keys=True))
                                patrol_candidates.append((date, durable_path))
                                continue
                            if (old.get("schema_version")
                                    != "research-v3-daily-status-v1"):
                                raise GateError(
                                    "TERMINAL_STATE_INVALID",
                                    "unknown terminal status schema")
                            # A known v1 status has no VersionId/SHA receipt.
                            # Re-enter the idempotent prepare/proof/commit path;
                            # an existing equivalent manifest is exact-read and
                            # returned as ALREADY_COMMITTED, then status is
                            # upgraded atomically to v2.
                    plan = pending_recovery_plan(
                        date, durable_path, live_dir, audit_root,
                        args.tagger_principal)
                    if plan is None:
                        patrol = refresh_policy_patrol_evidence(
                            durable_path, date, args, tag_env=tag_env,
                            publisher_environment=publisher_environment)
                        refresh_layered_single_writer_audit(
                            durable_path, date, args, patrol_evidence=patrol)
                        plan = prepare_date_plan(
                            date, live_dir, audit_root, args.tagger_principal,
                            receipt_root)
                    tagged, manifest_result = execute_date(
                        plan, args, tag_env=tag_env,
                        publisher_environment=publisher_environment)
                    status = {
                        "schema_version": TERMINAL_STATUS_SCHEMA,
                        "state": "V3_REFERENCE_PUBLISHED",
                        "date": date,
                        "durable_receipt_set_sha256": plan.durable_set_sha256,
                        "tagged_index": str(tagged),
                        "destination": args.dest,
                        "live_dir": str(live_dir),
                        "release_id": manifest_result["release_id"],
                        "prepared_plan_sha256":
                            manifest_result["prepared_plan_sha256"],
                        "manifest_commit_state": manifest_result["state"],
                        "manifest_object": manifest_result["manifest_object"],
                        "rfq": "OFF",
                        "completed_at_utc": _utc_now(),
                    }
                    _atomic_json(status_path, status)
                    print(json.dumps(status, sort_keys=True))
                    patrol_candidates.append((date, durable_path))
                except GateError as exc:
                    failure = {
                        "schema_version": "research-v3-daily-status-v1",
                        "state": "BLOCKED",
                        "date": date,
                        "code": exc.code,
                        "detail": exc.detail,
                        "rfq": "OFF",
                        "failed_at_utc": _utc_now(),
                    }
                    _record_attempt_failure(status_path, failure)
                    failures.append(failure)
                    print(json.dumps(failure, sort_keys=True), file=sys.stderr)
            if patrol_candidates:
                patrol_date, patrol_durable = max(
                    patrol_candidates, key=lambda row: row[0])
                patrol_status = state_root / "POLICY_PATROL_STATUS.json"
                try:
                    evidence = refresh_policy_patrol_evidence(
                        patrol_durable, patrol_date, args, tag_env=tag_env,
                        publisher_environment=publisher_environment)
                    _atomic_json(patrol_status, {
                        "schema_version": "research-v3-policy-patrol-status-v1",
                        "state": "POLICY_CANARY_PASS",
                        "date": patrol_date,
                        "policy_evidence": str(evidence),
                        "rfq": "OFF",
                        "checked_at_utc": _utc_now(),
                    })
                except GateError as exc:
                    failure = {
                        "schema_version": "research-v3-policy-patrol-status-v1",
                        "state": "POLICY_CANARY_BLOCKED",
                        "date": patrol_date,
                        "code": exc.code,
                        "detail": exc.detail,
                        "rfq": "OFF",
                        "failed_at_utc": _utc_now(),
                    }
                    _record_attempt_failure(patrol_status, failure)
                    failures.append(failure)
                    print(json.dumps(failure, sort_keys=True), file=sys.stderr)
            return 2 if failures else 0
        finally:
            os.close(lock_fd)
    except GateError as exc:
        print(f"V3_DAILY_REFUSED {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
