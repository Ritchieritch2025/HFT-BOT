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
from decimal import Decimal, InvalidOperation
import hashlib
import importlib.util
import json
import math
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
W2A_RELEASE_ID = "D3-W2A-2026-07-18.05"
W0_RELEASE_ID = "D3-W0-20260718-05"
W1_RELEASE_ID = "D3-W1-20260718-05"
PHASE = "OPEN_DISCOVERY"
INSTANCE_ID = "i-0e53d134dceffe166"
ROLE = "w09-research-runner"
PLAN_INSTALL_PATH = "/etc/w09/deep03/adopted-plan.md"
WRITE_ROOTS = {
    "/srv/w09-research/cache",
    "/srv/w09-research/automation",
    "/srv/w09-research/runs",
}
NETWORK_OPERATIONS = ["S3_READONLY_MANIFEST_AND_EXACT_VERSION"]
AUTHORIZED_TOOL_CLASSES = [
    "DUCKDB_LOCAL_ANALYTICS",
    "LOCAL_FILESYSTEM_IMMUTABLE_ARTIFACTS",
    "PYTHON3_PINNED_PAYLOAD",
    "SYSTEMD_ONESHOT",
]
AUTHORIZED_API_CLASSES = [
    "AWS_EC2_IMDSV2",
    "AWS_S3_GET_CANONICAL_OBJECT_VERSION_READONLY",
    "AWS_S3_GET_RELEASE_MANIFEST_READONLY",
    "AWS_S3_LIST_RELEASE_NAMESPACE_READONLY",
]
AUTHORIZED_CREDENTIAL_CLASSES = [
    "W09_INSTANCE_PROFILE_EPHEMERAL_ONLY",
]
REQUIRED_PREREQUISITE_HASHES = {
    "independent_plan_audit",
    "prior_exposure_ledger",
    "w09_exact_version_read",
    "w1_data_quality",
    "w1_completion",
    "w1_input_manifest",
    "w1_split_seal",
}
REQUIRED_NAMED_SUPERSESSION = "SECTION_4_15_1_PRE_READER_FEED_GATE"
W1_COMPLETION_SCHEMA = "deep03-w1-exploratory-precheck-completion-v1"
W1_DQ_SCHEMA = "deep03-w1-data-quality-receipt-v1"
W1_ARTIFACT_KEYS = {
    "INPUT_MANIFEST.json",
    "DATA_QUALITY_RECEIPT.json",
    "PRIOR_EXPOSURE_LEDGER.jsonl",
    "SPLIT_MANIFEST_OPEN_DISCOVERY.json",
}
W1_ARTIFACT_PREREQUISITES = {
    "INPUT_MANIFEST.json": "w1_input_manifest",
    "DATA_QUALITY_RECEIPT.json": "w1_data_quality",
    "PRIOR_EXPOSURE_LEDGER.jsonl": "prior_exposure_ledger",
    "SPLIT_MANIFEST_OPEN_DISCOVERY.json": "w1_split_seal",
}
W1_CANARY_STATE = "W09_V3_EXPLORATORY_QUERY_CANARY_PASS"
W1_EXACT_RELEASE_COUNT = 8
EXPECTED_EVIDENCE_TIER = "SEALED_DEGRADED_EVIDENCE"
EXPECTED_OBJECT_COUNT = 2657
EXPECTED_OBJECT_BYTES = 29473216651
W09_EFFECTIVE_RUNNING_USD_PER_HOUR = Decimal("0.50918")
W09_MAX_SPENDING_CAP_USD = Decimal("15")
AUTHORIZED_METHOD_SCOPE = {
    "D3-B01-MARKOUT": "PARTIAL_DESCRIPTIVE_ONLY",
    "D3-B02-ONESIDE": "PARTIAL_DESCRIPTIVE_ONLY",
    "D3-B03-XMKT": "NOT_ESTIMABLE_PREFLIGHT_ONLY",
    "D3-B04-RHYTHM": "PARTIAL_DESCRIPTIVE_ONLY",
}
W0_RELEASE_RE = re.compile(r"^D3-W0-[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
W1_RELEASE_RE = re.compile(r"^D3-W1-[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
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
    "s3_write_permission",
    "full_w1_completion_claim",
    "full_w2a_completion_claim",
    "post_research_hook_permission",
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


def _canonical_json_bytes(value: Any, label: str) -> bytes:
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
        raise AuthorityError("%s is not canonical-JSON compatible" % label) from exc


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


def _exact_string_list(value: Any, expected: list[str], label: str) -> None:
    if value != expected:
        raise AuthorityError("%s differs from the fixed narrow release scope" % label)


def _required_sha_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != REQUIRED_PREREQUISITE_HASHES:
        raise AuthorityError("prerequisite_receipt_sha256s has incomplete keys")
    for label, digest in value.items():
        if not isinstance(label, str) or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise AuthorityError("invalid prerequisite receipt SHA-256: %s" % label)
    return dict(value)


def _active_prompt(authority: dict[str, Any]) -> None:
    state = authority.get("active_prompt_state")
    path = authority.get("active_prompt_path")
    digest = authority.get("active_prompt_sha256")
    if state == "EXPLICIT_NONE":
        if path is not None or digest is not None:
            raise AuthorityError("explicit-none active prompt must have null path/SHA")
        return
    if state != "BOUND":
        raise AuthorityError("active_prompt_state must be BOUND or EXPLICIT_NONE")
    if not isinstance(path, str) or not path.startswith("/") or not path:
        raise AuthorityError("bound active prompt path must be absolute")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise AuthorityError("bound active prompt SHA-256 is invalid")


def _source_identity(authority: dict[str, Any]) -> tuple[str, str | None, str]:
    state = authority.get("authorized_source_state")
    branch = authority.get("authorized_branch")
    worktree = authority.get("authorized_worktree")
    if (
        not isinstance(worktree, str)
        or not worktree.startswith("/")
        or len(worktree) > 4096
        or any(ord(character) < 0x20 for character in worktree)
    ):
        raise AuthorityError("authorized_worktree is invalid")
    if state == "DETACHED_EXACT_COMMIT":
        if branch is not None:
            raise AuthorityError(
                "detached exact-commit source identity must have null authorized_branch"
            )
        base_commit = authority.get("base_commit")
        if not isinstance(base_commit, str) or COMMIT_RE.fullmatch(base_commit) is None:
            raise AuthorityError("detached source base_commit is invalid")
        return state, None, worktree
    if state != "NAMED_BRANCH":
        raise AuthorityError(
            "authorized_source_state must be DETACHED_EXACT_COMMIT or NAMED_BRANCH"
        )
    if (
        not isinstance(branch, str)
        or not branch
        or len(branch) > 255
        or any(ord(character) < 0x20 for character in branch)
    ):
        raise AuthorityError("authorized_branch is invalid for named-branch source")
    return state, branch, worktree


def _sha_map(value: Any, expected: set[str], label: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != expected:
        raise AuthorityError("%s has incomplete keys" % label)
    for name, digest in value.items():
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise AuthorityError("%s has invalid SHA-256: %s" % (label, name))
    return dict(value)


def _validate_w1_dq(
    value: Any,
    *,
    release_ids: list[str],
) -> None:
    if not isinstance(value, dict):
        raise AuthorityError("embedded W1 data-quality receipt is not an object")
    fixed = {
        "schema_version": W1_DQ_SCHEMA,
        "state": "W1_DQ_PASS_FOR_EXPLORATORY_ONLY",
        "mode": MODE,
        "evidence_tier": EXPECTED_EVIDENCE_TIER,
        "object_count": EXPECTED_OBJECT_COUNT,
        "object_bytes": EXPECTED_OBJECT_BYTES,
        "strict_acceptance_claimed": False,
        "exact_version_local_verification": "PASS",
        "holdout_opened": False,
        "rfq": "OFF_AND_ABSENT",
    }
    for field, expected in fixed.items():
        if value.get(field) != expected:
            raise AuthorityError("embedded W1 DQ field mismatch: %s" % field)
    if value.get("release_ids") != release_ids:
        raise AuthorityError("embedded W1 DQ release IDs differ from authority")
    if value.get("release_count") != W1_EXACT_RELEASE_COUNT:
        raise AuthorityError("embedded W1 DQ must bind exactly eight releases")
    if value.get("evidence_tier_counts") != {
        EXPECTED_EVIDENCE_TIER: W1_EXACT_RELEASE_COUNT
    }:
        raise AuthorityError("embedded W1 DQ evidence tier counts differ")
    canaries = value.get("canaries")
    if not isinstance(canaries, list) or len(canaries) != W1_EXACT_RELEASE_COUNT:
        raise AuthorityError("embedded W1 DQ must contain exactly eight canaries")
    observed: list[str] = []
    for canary in canaries:
        if not isinstance(canary, dict):
            raise AuthorityError("embedded W1 DQ canary is not an object")
        release_id = canary.get("release_id")
        observed.append(release_id)
        date = release_id.split("__", 1)[0] if isinstance(release_id, str) else None
        if (
            canary.get("state") != W1_CANARY_STATE
            or canary.get("date") != date
            or canary.get("evidence_tier") != EXPECTED_EVIDENCE_TIER
            or canary.get("strict_acceptance_claimed") is not False
            or canary.get("rfq") != "OFF"
            or not isinstance(canary.get("sha256"), str)
            or SHA256_RE.fullmatch(canary["sha256"]) is None
            or not isinstance(canary.get("table_count"), int)
            or isinstance(canary.get("table_count"), bool)
            or canary["table_count"] < 1
        ):
            raise AuthorityError("embedded W1 DQ canary contract mismatch")
    if observed != release_ids:
        raise AuthorityError("embedded W1 DQ canaries differ from exact release order")


def _validate_w1_completion(
    value: dict[str, Any],
    *,
    plan_sha: str,
    audit_sha: str,
    w0_release_id: str,
    w0_release_sha: str,
    w1_release_id: str,
    w1_release_sha: str,
    release_ids: list[str],
    prerequisite_hashes: dict[str, str],
) -> dict[str, str]:
    fixed = {
        "schema_version": W1_COMPLETION_SCHEMA,
        "state": "W1_COMPLETE_EXPLORATORY_PRECHECK",
        "mode": MODE,
        "adopted_plan_sha256": plan_sha,
        "audit_sha256": audit_sha,
        "w0_release_id": w0_release_id,
        "w0_release_sha256": w0_release_sha,
        "w1_release_id": w1_release_id,
        "w1_release_sha256": w1_release_sha,
        "authorized_input_release_ids": release_ids,
        "release_count": W1_EXACT_RELEASE_COUNT,
        "evidence_tier": EXPECTED_EVIDENCE_TIER,
        "object_count": EXPECTED_OBJECT_COUNT,
        "object_bytes": EXPECTED_OBJECT_BYTES,
        "all_inputs_prior_exposed": True,
        "holdout_opened": False,
        "strict_acceptance_claimed": False,
        "research_execution_started": False,
        "rfq": "OFF_AND_ABSENT",
        "network_reads_during_preflight": 0,
        "s3_writes": 0,
    }
    for field, expected in fixed.items():
        if value.get(field) != expected:
            raise AuthorityError("W1_COMPLETE field mismatch: %s" % field)
    artifacts = _sha_map(
        value.get("artifacts_sha256"),
        W1_ARTIFACT_KEYS,
        "W1_COMPLETE artifacts_sha256",
    )
    for artifact_name, prerequisite_name in W1_ARTIFACT_PREREQUISITES.items():
        if artifacts[artifact_name] != prerequisite_hashes[prerequisite_name]:
            raise AuthorityError(
                "W1 artifact differs from prerequisite binding: %s" % artifact_name
            )
    dq = value.get("embedded_data_quality_receipt")
    _validate_w1_dq(dq, release_ids=release_ids)
    dq_sha = hashlib.sha256(
        _canonical_json_bytes(dq, "embedded W1 data-quality receipt")
    ).hexdigest()
    if dq_sha != artifacts["DATA_QUALITY_RECEIPT.json"]:
        raise AuthorityError("embedded W1 DQ bytes differ from artifact binding")
    if prerequisite_hashes["w09_exact_version_read"] != dq_sha:
        raise AuthorityError(
            "w09_exact_version_read must bind the composite W1 DQ receipt"
        )
    return artifacts


def _upstream_release(
    authority: dict[str, Any], *, prefix: str, pattern: re.Pattern[str]
) -> tuple[str, str]:
    release_id = authority.get("%s_release_id" % prefix)
    release_sha = authority.get("%s_release_sha256" % prefix)
    if not isinstance(release_id, str) or pattern.fullmatch(release_id) is None:
        raise AuthorityError("%s release ID is invalid" % prefix.upper())
    if not isinstance(release_sha, str) or SHA256_RE.fullmatch(release_sha) is None:
        raise AuthorityError("%s release SHA-256 is invalid" % prefix.upper())
    return release_id, release_sha


def _one_shot_arm_module():
    try:
        import deep03_one_shot_arm

        return deep03_one_shot_arm
    except ModuleNotFoundError:
        source = Path(__file__).resolve().with_name("deep03_one_shot_arm.py")
        if not source.is_file():
            raise AuthorityError("pinned one-shot ARM module is unavailable")
        spec = importlib.util.spec_from_file_location(
            "deep03_one_shot_arm_gate_fallback", source
        )
        if spec is None or spec.loader is None:
            raise AuthorityError("pinned one-shot ARM module cannot be loaded")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def validate_authority_bundle(
    *,
    authority_path: Path,
    arm_path: Path,
    plan_path: Path,
    runtime_commit_path: Path,
    audit_path: Path,
    w0_release_path: Path,
    w1_release_path: Path,
    w1_complete_path: Path,
    expected_owner_uid: int = 0,
    now: dt.datetime | None = None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
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
    audit_raw = _secure_bytes(
        Path(audit_path),
        label="independent audit",
        max_bytes=16 * 1024 * 1024,
        expected_owner_uid=expected_owner_uid,
    )
    w0_release_raw = _secure_bytes(
        Path(w0_release_path),
        label="D3-W0 release",
        max_bytes=1024 * 1024,
        expected_owner_uid=expected_owner_uid,
    )
    w1_release_raw = _secure_bytes(
        Path(w1_release_path),
        label="D3-W1 release",
        max_bytes=1024 * 1024,
        expected_owner_uid=expected_owner_uid,
    )
    w1_complete_raw = _secure_bytes(
        Path(w1_complete_path),
        label="W1_COMPLETE receipt",
        max_bytes=4 * 1024 * 1024,
        expected_owner_uid=expected_owner_uid,
    )
    authority = _json(authority_raw, "AUTHORITY.json")
    arm = _json(arm_raw, "execution arm")
    w0_release_document = _json(w0_release_raw, "D3-W0 release")
    w1_release_document = _json(w1_release_raw, "D3-W1 release")
    w1_complete_document = _json(w1_complete_raw, "W1_COMPLETE receipt")
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
        "expected_evidence_tier": EXPECTED_EVIDENCE_TIER,
        "expected_object_count": EXPECTED_OBJECT_COUNT,
        "expected_object_bytes": EXPECTED_OBJECT_BYTES,
        "adopted_plan_path": PLAN_INSTALL_PATH,
        "allowed_network_operations": NETWORK_OPERATIONS,
    }
    for field, expected in fixed.items():
        if authority.get(field) != expected:
            raise AuthorityError("AUTHORITY.json field mismatch: %s" % field)
    release_id = authority.get("release_id")
    if release_id != W2A_RELEASE_ID:
        raise AuthorityError("authority release_id is not an exact D3-W2A release")
    for field in FALSE_AUTHORITY_FIELDS:
        if authority.get(field) is not False:
            raise AuthorityError("authority field must be explicit false: %s" % field)
    _active_prompt(authority)
    named_supersessions = authority.get("named_supersessions")
    if (
        not isinstance(named_supersessions, list)
        or not named_supersessions
        or any(not isinstance(item, str) or not item for item in named_supersessions)
        or len(named_supersessions) != len(set(named_supersessions))
        or REQUIRED_NAMED_SUPERSESSION not in named_supersessions
    ):
        raise AuthorityError(
            "named_supersessions must include the section 4.15.1 pre-reader feed gate"
        )
    source_state, source_branch, source_worktree = _source_identity(authority)
    _exact_string_list(
        authority.get("authorized_tool_classes"),
        AUTHORIZED_TOOL_CLASSES,
        "authorized_tool_classes",
    )
    _exact_string_list(
        authority.get("authorized_api_classes"),
        AUTHORIZED_API_CLASSES,
        "authorized_api_classes",
    )
    _exact_string_list(
        authority.get("authorized_credential_classes"),
        AUTHORIZED_CREDENTIAL_CLASSES,
        "authorized_credential_classes",
    )
    prerequisite_hashes = _required_sha_map(
        authority.get("prerequisite_receipt_sha256s")
    )
    audit_sha = authority.get("audit_sha256")
    if not isinstance(audit_sha, str) or SHA256_RE.fullmatch(audit_sha) is None:
        raise AuthorityError("audit_sha256 is invalid")
    if prerequisite_hashes["independent_plan_audit"] != audit_sha:
        raise AuthorityError("audit_sha256 differs from prerequisite audit binding")
    if hashlib.sha256(audit_raw).hexdigest() != audit_sha:
        raise AuthorityError("independent audit bytes differ from authority")
    audit_verdict = authority.get("independent_audit_verdict")
    if audit_verdict not in {
        "PASS_FOR_RELEASE_DRAFTING",
        "PASS_WITH_EXPLICIT_BLOCKERS",
    }:
        raise AuthorityError("independent audit verdict does not permit release drafting")
    w0_release_id, w0_release_sha = _upstream_release(
        authority, prefix="w0", pattern=W0_RELEASE_RE
    )
    w1_release_id, w1_release_sha = _upstream_release(
        authority, prefix="w1", pattern=W1_RELEASE_RE
    )
    if w0_release_id != W0_RELEASE_ID:
        raise AuthorityError("W0 release ID differs from the fixed .05 release")
    if w1_release_id != W1_RELEASE_ID:
        raise AuthorityError("W1 release ID differs from the fixed .05 release")
    if hashlib.sha256(w0_release_raw).hexdigest() != w0_release_sha:
        raise AuthorityError("D3-W0 release bytes differ from authority")
    if hashlib.sha256(w1_release_raw).hexdigest() != w1_release_sha:
        raise AuthorityError("D3-W1 release bytes differ from authority")
    if w0_release_document.get("release_id") != w0_release_id:
        raise AuthorityError("D3-W0 release document ID mismatch")
    if w1_release_document.get("release_id") != w1_release_id:
        raise AuthorityError("D3-W1 release document ID mismatch")
    w0_fixed = {
        "schema_version": "deep03-w0-release-v1",
        "state": "RELEASE_CANDIDATE",
        "adopted_plan_sha256": authority.get("adopted_plan_sha256"),
        "audit_sha256": audit_sha,
        "runtime_commit": authority.get("base_commit"),
        "expected_evidence_tier": EXPECTED_EVIDENCE_TIER,
        "expected_object_count": EXPECTED_OBJECT_COUNT,
        "expected_object_bytes": EXPECTED_OBJECT_BYTES,
        "research_execution_authority": False,
    }
    for field, expected in w0_fixed.items():
        if w0_release_document.get(field) != expected:
            raise AuthorityError("D3-W0 release document binding mismatch: %s" % field)
    w1_fixed = {
        "schema_version": "deep03-w1-release-v1",
        "state": "RELEASE_CANDIDATE",
        "w0_release_id": w0_release_id,
        "w0_release_sha256": w0_release_sha,
        "adopted_plan_sha256": authority.get("adopted_plan_sha256"),
        "audit_sha256": audit_sha,
        "runtime_commit": authority.get("base_commit"),
        "mode": MODE,
        "expected_evidence_tier": EXPECTED_EVIDENCE_TIER,
        "expected_object_count": EXPECTED_OBJECT_COUNT,
        "expected_object_bytes": EXPECTED_OBJECT_BYTES,
        "rfq_included": False,
        "strict_acceptance_claimed": False,
        "holdout_opened": False,
        "research_execution_authority": False,
    }
    for field, expected in w1_fixed.items():
        if w1_release_document.get(field) != expected:
            raise AuthorityError("D3-W1 release document binding mismatch: %s" % field)
    if (
        hashlib.sha256(w1_complete_raw).hexdigest()
        != prerequisite_hashes["w1_completion"]
    ):
        raise AuthorityError("W1_COMPLETE bytes differ from prerequisite binding")
    if authority.get("session_count") != 1:
        raise AuthorityError("session_count must be exactly 1")
    if authority.get("authorized_method_scope") != AUTHORIZED_METHOD_SCOPE:
        raise AuthorityError("authorized method scope is not the partial W2A scope")
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
    max_runtime = authority.get("max_runtime_seconds")
    if (
        not isinstance(max_runtime, int)
        or isinstance(max_runtime, bool)
        or max_runtime <= 0
        or max_runtime > 86400
    ):
        raise AuthorityError("max_runtime_seconds must be in [1,86400]")
    spending_cap = authority.get("spending_cap_usd")
    if (
        not isinstance(spending_cap, (int, float))
        or isinstance(spending_cap, bool)
        or (isinstance(spending_cap, float) and not math.isfinite(spending_cap))
    ):
        raise AuthorityError("finite numeric W09 spending_cap_usd was not authorized")
    try:
        spending_cap_decimal = Decimal(str(spending_cap))
    except InvalidOperation as exc:
        raise AuthorityError("spending_cap_usd is not a decimal amount") from exc
    required_runtime_cost = (
        Decimal(max_runtime) * W09_EFFECTIVE_RUNNING_USD_PER_HOUR / Decimal(3600)
    )
    if spending_cap_decimal <= 0 or spending_cap_decimal > W09_MAX_SPENDING_CAP_USD:
        raise AuthorityError("spending_cap_usd must be in (0,15]")
    if spending_cap_decimal < required_runtime_cost:
        raise AuthorityError(
            "spending_cap_usd does not cover max_runtime_seconds at $0.50918/hour"
        )
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
    if len(release_ids) != W1_EXACT_RELEASE_COUNT:
        raise AuthorityError("Deep03 W1/W2A authority must bind exactly eight releases")
    if w1_release_document.get("authorized_input_release_ids") != release_ids:
        raise AuthorityError("D3-W1 release input IDs differ from authority")
    if w1_release_document.get("authorized_input_dates") != [
        release_id.split("__", 1)[0] for release_id in release_ids
    ]:
        raise AuthorityError("D3-W1 release input dates differ from authority")
    w1_artifact_hashes = _validate_w1_completion(
        w1_complete_document,
        plan_sha=plan_sha,
        audit_sha=audit_sha,
        w0_release_id=w0_release_id,
        w0_release_sha=w0_release_sha,
        w1_release_id=w1_release_id,
        w1_release_sha=w1_release_sha,
        release_ids=release_ids,
        prerequisite_hashes=prerequisite_hashes,
    )

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

    result = {
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
        "expected_evidence_tier": EXPECTED_EVIDENCE_TIER,
        "expected_object_count": EXPECTED_OBJECT_COUNT,
        "expected_object_bytes": EXPECTED_OBJECT_BYTES,
        "spending_cap_usd": spending_cap,
        "w09_effective_running_usd_per_hour": float(
            W09_EFFECTIVE_RUNNING_USD_PER_HOUR
        ),
        "required_max_runtime_cost_usd": float(required_runtime_cost),
        "max_runtime_seconds": max_runtime,
        "effective_runtime_seconds": min(max_runtime, active_window_seconds),
        "expires_at_utc": authority["expires_at_utc"],
        "arm_expires_at_utc": arm["expires_at_utc"],
        "active_prompt_state": authority["active_prompt_state"],
        "active_prompt_path": authority["active_prompt_path"],
        "active_prompt_sha256": authority["active_prompt_sha256"],
        "operator_text_sha256": authority["operator_text_sha256"],
        "named_supersessions": list(named_supersessions),
        "authorized_source_state": source_state,
        "authorized_branch": source_branch,
        "authorized_worktree": source_worktree,
        "authorized_tool_classes": list(AUTHORIZED_TOOL_CLASSES),
        "authorized_api_classes": list(AUTHORIZED_API_CLASSES),
        "authorized_credential_classes": list(AUTHORIZED_CREDENTIAL_CLASSES),
        "prerequisite_receipt_sha256s": prerequisite_hashes,
        "w1_artifacts_sha256": w1_artifact_hashes,
        "w09_exact_version_read_evidence": {
            "binding_kind": "COMPOSITE_W1_DATA_QUALITY_RECEIPT_SHA256",
            "sha256": prerequisite_hashes["w09_exact_version_read"],
            "exact_version_local_verification": "PASS",
            "canary_state": W1_CANARY_STATE,
            "canary_count": W1_EXACT_RELEASE_COUNT,
            "evidence_tier": EXPECTED_EVIDENCE_TIER,
            "object_count": EXPECTED_OBJECT_COUNT,
            "object_bytes": EXPECTED_OBJECT_BYTES,
        },
        "audit_sha256": audit_sha,
        "independent_audit_verdict": audit_verdict,
        "w0_release_id": w0_release_id,
        "w0_release_sha256": w0_release_sha,
        "w1_release_id": w1_release_id,
        "w1_release_sha256": w1_release_sha,
        "session_count": 1,
        "authorized_method_scope": dict(AUTHORIZED_METHOD_SCOPE),
    }
    artifacts = {
        "AUTHORITY.json": authority_raw,
        "EXECUTION_ARM.json": arm_raw,
        "ADOPTED_PLAN.md": plan_raw,
        "BASE_COMMIT.txt": runtime_raw,
        "AUDIT.md": audit_raw,
        "D3_W0_RELEASE.json": w0_release_raw,
        "D3_W1_RELEASE.json": w1_release_raw,
        "W1_COMPLETE.json": w1_complete_raw,
    }
    return result, artifacts


def validate_authority(
    *,
    authority_path: Path,
    arm_path: Path,
    plan_path: Path,
    runtime_commit_path: Path,
    audit_path: Path,
    w0_release_path: Path,
    w1_release_path: Path,
    w1_complete_path: Path,
    expected_owner_uid: int = 0,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    result, _artifacts = validate_authority_bundle(
        authority_path=authority_path,
        arm_path=arm_path,
        plan_path=plan_path,
        runtime_commit_path=runtime_commit_path,
        audit_path=audit_path,
        w0_release_path=w0_release_path,
        w1_release_path=w1_release_path,
        w1_complete_path=w1_complete_path,
        expected_owner_uid=expected_owner_uid,
        now=now,
    )
    return result


def validate_claimed_authority_bundle(
    *,
    authority_path: Path,
    arm_path: Path,
    arm_claim_root: Path,
    plan_path: Path,
    runtime_commit_path: Path,
    audit_path: Path,
    w0_release_path: Path,
    w1_release_path: Path,
    w1_complete_path: Path,
    expected_owner_uid: int = 0,
    now: dt.datetime | None = None,
    invocation_id: str | None = None,
    proc_cgroup_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Validate the static release and its current systemd one-shot claim."""
    result, artifacts = validate_authority_bundle(
        authority_path=authority_path,
        arm_path=arm_path,
        plan_path=plan_path,
        runtime_commit_path=runtime_commit_path,
        audit_path=audit_path,
        w0_release_path=w0_release_path,
        w1_release_path=w1_release_path,
        w1_complete_path=w1_complete_path,
        expected_owner_uid=expected_owner_uid,
        now=now,
    )
    one_shot = _one_shot_arm_module()
    exact_invocation = invocation_id or os.environ.get("INVOCATION_ID", "")
    identity = {
        "release_id": result["release_id"],
        "authority_sha256": result["authority_sha256"],
        "arm_sha256": result["arm_sha256"],
    }
    try:
        claim_kwargs = {
            "claim_root": Path(arm_claim_root),
            "identity": identity,
            "invocation_id": exact_invocation,
            "service_unit": one_shot.SERVICE_UNIT,
            "expected_owner_uid": expected_owner_uid,
        }
        if proc_cgroup_path is not None:
            claim_kwargs["proc_cgroup_path"] = Path(proc_cgroup_path)
        claim, claim_raw = one_shot.validate_active_claim(
            **claim_kwargs,
        )
    except one_shot.OneShotArmError as exc:
        raise AuthorityError("one-shot execution claim refused: %s" % exc) from exc
    claimed = {
        **result,
        "arm_claim_state": "ACTIVE",
        "arm_claim_sha256": hashlib.sha256(claim_raw).hexdigest(),
        "arm_claim_invocation_id": claim["invocation_id"],
        "arm_claim_service_unit": claim["service_unit"],
        "arm_claim_service_cgroup": claim["service_cgroup"],
    }
    return claimed, {**artifacts, "ARM_CLAIM.json": claim_raw}


def validate_claimed_authority(**kwargs: Any) -> dict[str, Any]:
    result, _artifacts = validate_claimed_authority_bundle(**kwargs)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authority", required=True, type=Path)
    parser.add_argument("--arm-file", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--runtime-commit", required=True, type=Path)
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--w0-release", required=True, type=Path)
    parser.add_argument("--w1-release", required=True, type=Path)
    parser.add_argument("--w1-complete", required=True, type=Path)
    parser.add_argument("--arm-claim-root", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        validator = (
            validate_claimed_authority
            if args.arm_claim_root is not None
            else validate_authority
        )
        kwargs = {
            "authority_path": args.authority,
            "arm_path": args.arm_file,
            "plan_path": args.plan,
            "runtime_commit_path": args.runtime_commit,
            "audit_path": args.audit,
            "w0_release_path": args.w0_release,
            "w1_release_path": args.w1_release,
            "w1_complete_path": args.w1_complete,
        }
        if args.arm_claim_root is not None:
            kwargs["arm_claim_root"] = args.arm_claim_root
        result = validator(**kwargs)
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
