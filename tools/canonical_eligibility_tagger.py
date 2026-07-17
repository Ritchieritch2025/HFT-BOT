#!/usr/bin/env python3
"""Exact-version, single-writer research eligibility tagger.

This is the only module in the canonical receipt flow that can mutate S3
object tags.  It consumes a *durable byte receipt index*, exact-GETs the
receipt object named by that index, and revalidates its immutable identity.
It never trusts a local receipt body and never re-downloads the large data
objects whose full-byte attestations are already carried by the receipt.

Before the first tag write, every target exact version is preflighted with
``GetObjectVersionTagging``.  Existing tags are retained, conflicting
eligibility values and the ten-tag limit fail closed, and RFQ/raw objects are
not eligible by default.  Sealed RFQ objects require an explicit paired
evidence option and receive both eligibility and channel tags.  A successful
run creates a new content-addressed
durable receipt with ``TAGGED_VERIFIED`` eligibility state; it never replaces
the byte receipt.

The required single-writer audit artifact is a JSON object with this shape::

  {
    "schema_version": "canonical-eligibility-single-writer-audit-v1",
    "state": "SINGLE_WRITER_VERIFIED",
    "date": "YYYY-MM-DD",
    "bucket": "bucket-name",
    "receipt_set_sha256": "<durable byte receipt digest>",
    "tag_key": "research-eligible",
    "dedicated_tagger_principal": "arn:...",
    "dedicated_tagger_sts_caller_arn": "arn:aws:sts::...:assumed-role/.../...",
    "exclusive_exact_version_writer": true,
    "versionless_tagging_denied": true,
    "other_automation_tag_writers_denied": true,
    "audited_at_utc": "...Z",
    "auditor": "...",
    "policy_evidence_size": 1234,
    "policy_evidence_sha256": "<sha256>"
  }

The artifact is an operator/audit gate, not a substitute for the IAM and
bucket-policy controls that it records.
"""
import argparse
import copy
import hashlib
import json
import os
import pathlib
import re
import subprocess
import tempfile

import canonical_receipt_control as crc
import canonical_receipts as cr
import git_provenance as gp
import warehouse_common as wc


AUDIT_SCHEMA = "canonical-eligibility-single-writer-audit-v1"
AUDIT_STATE = "SINGLE_WRITER_VERIFIED"
TAG_KEY = "research-eligible"
TAG_VALUE = "true"
RFQ_TAG_KEY = "research-channel"
RFQ_TAG_VALUE = "rfq"
MAX_S3_TAGS = 10
TAGGED_PHASE = "TAGGED_ELIGIBILITY_VERIFIED"
PENDING_INDEX_SCHEMA = "canonical-tagged-receipt-pending-index-v1"
PENDING_INDEX_STATE = "TAGGED_RECEIPT_OBJECT_TAG_PENDING"
MAX_DURABLE_INDEX_BYTES = 1024 * 1024
MAX_SINGLE_WRITER_AUDIT_BYTES = 64 * 1024
MAX_POLICY_EVIDENCE_BYTES = 1024 * 1024
MAX_RFQ_ELIGIBILITY_EVIDENCE_BYTES = 4 * 1024 * 1024
MAX_STS_IDENTITY_BYTES = 64 * 1024
MAX_AUDIT_AGE_SECONDS = 24 * 60 * 60
MAX_AUDIT_FUTURE_SKEW_SECONDS = 5 * 60
RFQ_EVIDENCE_SCHEMA = "canonical-rfq-eligibility-evidence-v1"
RFQ_EVIDENCE_STATE = "SEALED_RFQ_ELIGIBILITY_VERIFIED"
RFQ_BINDING_SCHEMA = "canonical-rfq-eligibility-binding-v1"
RFQ_BINDING_STATE = "ELIGIBLE_SEALED_REFERENCE"
RFQ_DUAL_TAG_STATE = "DUAL_TAGGED_VERIFIED"
RFQ_EXPOSURE_POLICY = "RESEARCH_ELIGIBLE_SEALED_RFQ"
RFQ_PROJECTION_FIELDS = (
    "logical_source_key", "bucket", "key", "VersionId", "size", "sha256",
    "last_modified_utc",
)
RFQ_EVIDENCE_FIELDS = frozenset({
    "schema_version", "state", "date", "bucket", "prefix",
    "byte_receipt_set_sha256", "seal_sha256", "evidence_tier",
    "integrity_state", "quarantine_state", "repair_branch_state",
    "rfq_exact_set_sha256", "objects", "decided_at_utc", "decider",
    "source_evidence_sha256",
})
RFQ_BINDING_FIELDS = frozenset({
    "schema_version", "state", "source", "seal_sha256",
    "rfq_exact_set_sha256", "eligibility_evidence_sha256",
    "evidence_tier", "integrity_state", "quarantine_state",
    "repair_branch_state",
})
TAGGER_PROVENANCE_PATHS = cr.CANONICAL_RECEIPT_PROVENANCE_PATHS + (
    "tools/canonical_eligibility_tagger.py",
)

_IAM_USER_ARN_RE = re.compile(
    r"^arn:(aws(?:-us-gov|-cn)?):iam::([0-9]{12}):"
    r"user/([A-Za-z0-9+=,.@_/-]+)$")
_IAM_ROLE_ARN_RE = re.compile(
    r"^arn:(aws(?:-us-gov|-cn)?):iam::([0-9]{12}):"
    r"role/([A-Za-z0-9+=,.@_-]+)$")
_STS_ROLE_ARN_RE = re.compile(
    r"^arn:(aws(?:-us-gov|-cn)?):sts::([0-9]{12}):"
    r"assumed-role/([A-Za-z0-9+=,.@_-]+)/([A-Za-z0-9+=,.@_-]+)$")


def _valid_sha256(value):
    return (isinstance(value, str) and len(value) == 64
            and all(char in "0123456789abcdef" for char in value))


def _mutation_code_commit():
    try:
        return gp.require_clean_head(wc.ROOT, TAGGER_PROVENANCE_PATHS)
    except gp.GitProvenanceError as exc:
        raise cr.ReceiptError(exc.code, exc.detail)


def _json_bytes(payload):
    return (json.dumps(payload, sort_keys=True, indent=2,
                       ensure_ascii=True).encode("utf-8") + b"\n")


def _read_bounded_nofollow(path, *, limit, code, label):
    """Freeze one regular file without following it or reading past a cap."""
    fd = None
    try:
        fd, before = cr._open_regular_nofollow(os.fspath(path))
        if before.st_size > limit:
            raise cr.ReceiptError(
                code, "%s is %d bytes; limit is %d" %
                (label, before.st_size, limit))
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, min(64 * 1024, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise cr.ReceiptError(
                    code, "%s exceeds %d bytes" % (label, limit))
        cr._assert_fd_and_path_stable(fd, os.fspath(path), before, total)
        return b"".join(chunks)
    except cr.ReceiptError as exc:
        if exc.code == code:
            raise
        raise cr.ReceiptError(code, "%s: %s" % (label, exc.detail))
    except (OSError, TypeError, ValueError) as exc:
        raise cr.ReceiptError(code, str(exc))
    finally:
        if fd is not None:
            os.close(fd)


def _reject_duplicate_pairs(pairs):
    parsed = {}
    for key, value in pairs:
        if key in parsed:
            raise ValueError("duplicate JSON key %r" % key)
        parsed[key] = value
    return parsed


def _parse_json_object(payload, code):
    try:
        parsed = json.loads(payload, object_pairs_hook=_reject_duplicate_pairs)
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise cr.ReceiptError(code, str(exc))
    if not isinstance(parsed, dict):
        raise cr.ReceiptError(code, "JSON root is not an object")
    return parsed


def _load_json_file(path, code, *, limit, label):
    payload = _read_bounded_nofollow(
        path, limit=limit, code=code, label=label)
    parsed = _parse_json_object(payload, code)
    return parsed, payload


def _validate_audit_freshness(audited_at_utc, now_utc):
    try:
        audited = cr._parse_utc(audited_at_utc, "audit audited_at_utc")
        now = cr._parse_utc(now_utc, "eligibility operation time")
    except cr.ReceiptError as exc:
        raise cr.ReceiptError("SINGLE_WRITER_AUDIT_INVALID", exc.detail)
    age = (now - audited).total_seconds()
    if age < -MAX_AUDIT_FUTURE_SKEW_SECONDS:
        raise cr.ReceiptError(
            "SINGLE_WRITER_AUDIT_STALE",
            "audit is %.0f seconds in the future" % -age)
    if age > MAX_AUDIT_AGE_SECONDS:
        raise cr.ReceiptError(
            "SINGLE_WRITER_AUDIT_STALE",
            "audit is %.0f seconds old; limit is %d" %
            (age, MAX_AUDIT_AGE_SECONDS))


def _validate_principal_spec(audit):
    """Return the exact STS caller ARN required by the audit artifact."""
    principal = audit.get("dedicated_tagger_principal")
    if not isinstance(principal, str):
        raise cr.ReceiptError(
            "SINGLE_WRITER_AUDIT_INVALID", "tagger principal is missing")
    user = _IAM_USER_ARN_RE.fullmatch(principal)
    if user:
        expected = audit.get("dedicated_tagger_sts_caller_arn", principal)
        if expected != principal:
            raise cr.ReceiptError(
                "SINGLE_WRITER_AUDIT_INVALID",
                "IAM user caller ARN must equal dedicated principal")
        return principal, user.group(2)
    role = _IAM_ROLE_ARN_RE.fullmatch(principal)
    if not role:
        raise cr.ReceiptError(
            "SINGLE_WRITER_AUDIT_INVALID",
            "dedicated principal must be an IAM user ARN or a pathless "
            "IAM role ARN")
    caller = audit.get("dedicated_tagger_sts_caller_arn")
    assumed = (_STS_ROLE_ARN_RE.fullmatch(caller)
               if isinstance(caller, str) else None)
    if (not assumed or assumed.group(1) != role.group(1)
            or assumed.group(2) != role.group(2)
            or assumed.group(3) != role.group(3)):
        raise cr.ReceiptError(
            "SINGLE_WRITER_AUDIT_INVALID",
            "IAM role requires an exact matching assumed-role caller ARN")
    return caller, role.group(2)


def load_single_writer_audit(path, policy_evidence_path, *, date, bucket,
                             receipt_set_sha256, now_utc,
                             include_sealed_rfq=False):
    """Validate and bind the explicit single-writer authorization evidence."""
    audit, raw = _load_json_file(
        path, "SINGLE_WRITER_AUDIT_INVALID",
        limit=MAX_SINGLE_WRITER_AUDIT_BYTES, label="single-writer audit")
    evidence = _read_bounded_nofollow(
        policy_evidence_path, limit=MAX_POLICY_EVIDENCE_BYTES,
        code="POLICY_EVIDENCE_INVALID", label="policy evidence")
    if not evidence:
        raise cr.ReceiptError(
            "POLICY_EVIDENCE_INVALID", "policy evidence is empty")
    fixed = {
        "schema_version": AUDIT_SCHEMA,
        "state": AUDIT_STATE,
        "date": date,
        "bucket": bucket,
        "receipt_set_sha256": receipt_set_sha256,
        "tag_key": TAG_KEY,
        "exclusive_exact_version_writer": True,
        "versionless_tagging_denied": True,
        "other_automation_tag_writers_denied": True,
    }
    for field, expected in fixed.items():
        if audit.get(field) != expected:
            raise cr.ReceiptError(
                "SINGLE_WRITER_AUDIT_INVALID",
                "%s=%r, expected %r" % (field, audit.get(field), expected))
    if include_sealed_rfq:
        rfq_fixed = {
            "rfq_tag_key": RFQ_TAG_KEY,
            "rfq_tag_value": RFQ_TAG_VALUE,
            "rfq_exact_version_scope_verified": True,
        }
        for field, expected in rfq_fixed.items():
            if audit.get(field) != expected:
                raise cr.ReceiptError(
                    "SINGLE_WRITER_AUDIT_INVALID",
                    "%s=%r, expected %r for RFQ mode" %
                    (field, audit.get(field), expected))
    for field in ("dedicated_tagger_principal", "auditor"):
        if not isinstance(audit.get(field), str) or not audit[field].strip():
            raise cr.ReceiptError(
                "SINGLE_WRITER_AUDIT_INVALID", "%s is missing" % field)
    _validate_principal_spec(audit)
    _validate_audit_freshness(audit.get("audited_at_utc"), now_utc)
    if not _valid_sha256(audit.get("policy_evidence_sha256")):
        raise cr.ReceiptError(
            "SINGLE_WRITER_AUDIT_INVALID", "policy evidence digest is invalid")
    evidence_size = audit.get("policy_evidence_size")
    if (not isinstance(evidence_size, int) or isinstance(evidence_size, bool)
            or evidence_size <= 0
            or evidence_size > MAX_POLICY_EVIDENCE_BYTES):
        raise cr.ReceiptError(
            "SINGLE_WRITER_AUDIT_INVALID", "policy evidence size is invalid")
    if (audit["policy_evidence_sha256"]
            != hashlib.sha256(evidence).hexdigest()
            or evidence_size != len(evidence)):
        raise cr.ReceiptError(
            "POLICY_EVIDENCE_MISMATCH",
            "policy evidence bytes do not match the audit binding")
    return audit, hashlib.sha256(raw).hexdigest()


def verify_ambient_caller(audit, identity):
    """Bind ambient AWS credentials to the principal audited for writes."""
    if not isinstance(identity, dict):
        raise cr.ReceiptError(
            "TAGGER_CALLER_IDENTITY_INVALID", "STS response is not an object")
    expected_arn, expected_account = _validate_principal_spec(audit)
    arn = identity.get("Arn")
    account = identity.get("Account")
    user_id = identity.get("UserId")
    if (arn != expected_arn or account != expected_account
            or not isinstance(user_id, str) or not user_id.strip()):
        raise cr.ReceiptError(
            "TAGGER_CALLER_MISMATCH",
            "ambient STS caller does not match the audited tagger principal")
    return arn


def _read_exact_receipt(index, reader, *, expected_bucket, expected_prefix):
    binding = index.get("receipt_object")
    if not isinstance(binding, dict):
        raise cr.ReceiptError(
            "DURABLE_INDEX_INVALID", "receipt_object binding is missing")
    bucket = binding.get("bucket")
    if not isinstance(bucket, str) or not bucket or "/" in bucket:
        raise cr.ReceiptError("DURABLE_INDEX_INVALID", "invalid receipt bucket")
    if expected_bucket is not None and bucket != expected_bucket:
        raise cr.ReceiptError("DURABLE_INDEX_INVALID", "receipt bucket mismatch")
    key = cr._safe_rel(binding.get("key"), "durable receipt key")
    version_id = binding.get("VersionId")
    size = binding.get("size")
    digest = binding.get("sha256")
    modified = binding.get("last_modified_utc")
    if (not crc._valid_version_id(version_id)
            or not isinstance(size, int) or isinstance(size, bool) or size < 0
            or not _valid_sha256(digest)
            or binding.get("verification_state")
            != "EXACT_VERSION_FULL_SHA256"):
        raise cr.ReceiptError(
            "DURABLE_INDEX_INVALID", "receipt exact-version binding is invalid")
    if size > crc.MAX_DURABLE_RECEIPT_BYTES:
        raise cr.ReceiptError(
            "DURABLE_RECEIPT_TOO_LARGE",
            "receipt is %d bytes; limit is %d" %
            (size, crc.MAX_DURABLE_RECEIPT_BYTES))
    if (not isinstance(modified, str)
            or modified != cr._canonical_utc(
                modified, "durable receipt LastModified")):
        raise cr.ReceiptError(
            "DURABLE_INDEX_INVALID", "receipt LastModified is invalid")
    if (index.get("receipt_payload_size") != size
            or index.get("receipt_payload_sha256") != digest):
        raise cr.ReceiptError(
            "DURABLE_INDEX_INVALID", "receipt payload binding mismatch")
    date = index["date"]
    set_sha = index["receipt_set_sha256"]
    suffix = ("control/canonical-receipts/v1/date=%s/receipt-%s.json" %
              (date, set_sha))
    if not key.endswith(suffix):
        raise cr.ReceiptError(
            "DURABLE_INDEX_INVALID", "receipt key does not bind its digest")
    prefix = key[:-len(suffix)].rstrip("/")
    if not prefix:
        raise cr.ReceiptError(
            "DURABLE_INDEX_INVALID", "canonical prefix is empty")
    prefix = cr._safe_rel(prefix, "canonical prefix")
    if expected_prefix is not None:
        expected_prefix = cr._safe_rel(
            expected_prefix.strip("/"), "expected canonical prefix")
        if prefix != expected_prefix:
            raise cr.ReceiptError(
                "DURABLE_INDEX_INVALID", "canonical prefix mismatch")
    try:
        head = reader.head(bucket, key, version_id)
    except Exception as exc:
        if isinstance(exc, cr.ReceiptError):
            raise
        raise cr.ReceiptError("DURABLE_RECEIPT_READ_FAILED", str(exc))
    if (not isinstance(head, dict)
            or head.get("VersionId") != version_id
            or head.get("ContentLength") != size
            or cr._canonical_utc(
                head.get("LastModified"), "receipt HEAD LastModified")
            != modified):
        raise cr.ReceiptError(
            "DURABLE_RECEIPT_READ_FAILED", "exact HEAD binding mismatch")
    with tempfile.TemporaryDirectory(
            prefix="eligibility-byte-receipt-") as temp_dir:
        path = os.path.join(temp_dir, "receipt.json")
        try:
            reader.get_exact(bucket, key, version_id, path)
            body = _read_bounded_nofollow(
                path, limit=crc.MAX_DURABLE_RECEIPT_BYTES,
                code="DURABLE_RECEIPT_READ_FAILED",
                label="downloaded durable receipt")
        except Exception as exc:
            if isinstance(exc, cr.ReceiptError):
                raise
            raise cr.ReceiptError("DURABLE_RECEIPT_READ_FAILED", str(exc))
    if len(body) != size or hashlib.sha256(body).hexdigest() != digest:
        raise cr.ReceiptError(
            "DURABLE_RECEIPT_READ_FAILED", "exact body size/SHA mismatch")
    receipt = _parse_json_object(body, "DURABLE_RECEIPT_INVALID")
    return receipt, prefix, body


def _validate_byte_parent(index, receipt, *, bucket, prefix,
                          receipt_body=None):
    """Require the byte index, exact receipt body and parent to be one set."""
    set_sha = receipt.get("receipt_set_sha256")
    binding = index.get("receipt_object")
    fixed_index = {
        "schema_version": crc.DURABLE_INDEX_SCHEMA,
        "state": crc.DURABLE_STATE,
        "complete": True,
        "prune_eligible": False,
        "date": receipt.get("date"),
        "receipt_set_sha256": set_sha,
    }
    if any(index.get(field) != value for field, value in fixed_index.items()):
        raise cr.ReceiptError(
            "BYTE_PARENT_MISMATCH", "byte index does not bind its receipt")
    if not isinstance(binding, dict):
        raise cr.ReceiptError(
            "BYTE_PARENT_MISMATCH", "byte receipt object binding is missing")
    expected_body = receipt_body if receipt_body is not None else \
        _json_bytes(receipt)
    if (binding.get("bucket") != bucket
            or index.get("receipt_payload_size") != len(expected_body)
            or index.get("receipt_payload_sha256")
            != hashlib.sha256(expected_body).hexdigest()
            or binding.get("size") != len(expected_body)
            or binding.get("sha256")
            != hashlib.sha256(expected_body).hexdigest()):
        raise cr.ReceiptError(
            "BYTE_PARENT_MISMATCH", "byte receipt payload binding mismatch")
    key = cr._safe_rel(binding.get("key"), "byte receipt key")
    expected_key = cr._join_key(
        prefix, "control", "canonical-receipts", "v1",
        "date=%s" % receipt.get("date"), "receipt-%s.json" % set_sha)
    if (key != expected_key or not crc._valid_version_id(
            binding.get("VersionId"))
            or binding.get("verification_state")
            != "EXACT_VERSION_FULL_SHA256"):
        raise cr.ReceiptError(
            "BYTE_PARENT_MISMATCH", "byte receipt exact object mismatch")
    try:
        modified = binding.get("last_modified_utc")
        if modified != cr._canonical_utc(
                modified, "byte receipt LastModified"):
            raise cr.ReceiptError(
                "BYTE_PARENT_MISMATCH", "non-canonical LastModified")
    except cr.ReceiptError as exc:
        if exc.code == "BYTE_PARENT_MISMATCH":
            raise
        raise cr.ReceiptError("BYTE_PARENT_MISMATCH", exc.detail)
    shadow = copy.deepcopy(receipt)
    shadow.update({
        "state": "RECEIPT_VERIFIED_SHADOW",
        "authority": "SHADOW_LOCAL_ONLY",
        "authoritative": False,
        "s3_published": False,
        "prune_eligible": False,
    })
    try:
        crc.validate_shadow_receipt(
            shadow, expected_bucket=bucket, expected_prefix=prefix)
    except cr.ReceiptError as exc:
        raise cr.ReceiptError("BYTE_PARENT_MISMATCH", exc.detail)
    return receipt


def load_durable_byte_receipt(index_path, reader, *, expected_bucket=None,
                              expected_prefix=None):
    """Authenticate a local index by exact-reading its remote receipt body."""
    index, _raw = _load_json_file(
        index_path, "DURABLE_INDEX_INVALID",
        limit=MAX_DURABLE_INDEX_BYTES, label="durable byte receipt index")
    date = index.get("date")
    cr._validate_date(date)
    set_sha = index.get("receipt_set_sha256")
    fixed = {
        "schema_version": crc.DURABLE_INDEX_SCHEMA,
        "state": crc.DURABLE_STATE,
        "complete": True,
        "prune_eligible": False,
    }
    if any(index.get(field) != value for field, value in fixed.items()):
        raise cr.ReceiptError(
            "DURABLE_INDEX_INVALID", "index authority/state mismatch")
    if not _valid_sha256(set_sha):
        raise cr.ReceiptError(
            "DURABLE_INDEX_INVALID", "receipt_set_sha256 is invalid")
    receipt, prefix, receipt_body = _read_exact_receipt(
        index, reader, expected_bucket=expected_bucket,
        expected_prefix=expected_prefix)
    receipt_fixed = {
        "schema_version": cr.RECEIPT_SCHEMA,
        "state": crc.DURABLE_STATE,
        "authority": crc.DURABLE_AUTHORITY,
        "authoritative": True,
        "s3_published": True,
        "prune_eligible": False,
        "date": date,
        "receipt_set_sha256": set_sha,
    }
    if (not isinstance(receipt, dict)
            or any(receipt.get(field) != value
                   for field, value in receipt_fixed.items())):
        raise cr.ReceiptError(
            "DURABLE_RECEIPT_INVALID", "receipt authority/state mismatch")
    try:
        _validate_byte_parent(
            index, receipt, bucket=index["receipt_object"]["bucket"],
            prefix=prefix, receipt_body=receipt_body)
    except cr.ReceiptError as exc:
        raise cr.ReceiptError("DURABLE_RECEIPT_INVALID", exc.detail)
    return index, receipt, prefix


def _is_rfq_object(obj):
    key = str(obj.get("key") or "")
    logical = str(obj.get("logical_source_key") or "")
    source_kind = str(obj.get("source_kind") or "")
    return (obj.get("channel") == "rfq"
            or source_kind.startswith("raw_rfq")
            or ("/raw/" in ("/" + key)
                and cr.RFQ_RE.match(os.path.basename(key)) is not None)
            or (logical.startswith("raw/")
                and cr.RFQ_RE.match(os.path.basename(logical)) is not None))


def _is_raw_object(obj, prefix):
    key = str(obj.get("key") or "")
    logical = str(obj.get("logical_source_key") or "")
    source_kind = str(obj.get("source_kind") or "")
    return (source_kind.startswith("raw")
            or key.startswith(prefix + "/raw/")
            or logical.startswith("raw/"))


def select_tag_targets(receipt, prefix):
    """Return the complete warehouse/control candidate set or fail closed."""
    targets = []
    for obj in receipt["objects"]:
        if obj.get("research_candidate") is not True:
            continue
        key = str(obj.get("key") or "")
        if _is_rfq_object(obj):
            raise cr.ReceiptError(
                "RFQ_TAGGING_FORBIDDEN",
                "%s is raw/RFQ" % obj.get("logical_source_key"))
        if _is_raw_object(obj, prefix):
            raise cr.ReceiptError(
                "RAW_TAGGING_FORBIDDEN",
                "%s is non-RFQ raw" % obj.get("logical_source_key"))
        if not (key.startswith(prefix + "/warehouse/")
                or key.startswith(prefix + "/control/")):
            raise cr.ReceiptError(
                "ELIGIBILITY_TARGET_FORBIDDEN", key)
        if str(obj.get("exposure_policy") or "").startswith("FORBIDDEN"):
            raise cr.ReceiptError(
                "ELIGIBILITY_TARGET_FORBIDDEN", key)
        if (not crc._valid_version_id(obj.get("VersionId"))
                or obj.get("verification_state")
                != "EXACT_VERSION_FULL_SHA256"
                or obj.get("durability_verified") is not True):
            raise cr.ReceiptError(
                "ELIGIBILITY_TARGET_INVALID", key)
        targets.append(obj)
    if not targets:
        raise cr.ReceiptError(
            "ELIGIBILITY_SET_EMPTY", "receipt has no research candidates")
    targets.sort(key=lambda obj: (
        obj["bucket"], obj["key"], obj["VersionId"]))
    return targets


def _rfq_projection(obj):
    return {field: obj.get(field) for field in RFQ_PROJECTION_FIELDS}


def _sort_rfq_projection(rows):
    return sorted(rows, key=lambda row: (
        row["logical_source_key"], row["bucket"], row["key"],
        row["VersionId"]))


def rfq_exact_set_sha256(objects):
    return cr.canonical_sha256(_sort_rfq_projection([
        _rfq_projection(obj) for obj in objects]))


def select_rfq_targets(receipt, prefix):
    """Return every seal-derived RFQ exact version in the byte receipt."""
    targets = []
    seal_sha = (receipt.get("seal") or {}).get("sha256")
    for obj in receipt.get("objects") or []:
        if not _is_rfq_object(obj):
            continue
        key = str(obj.get("key") or "")
        logical = str(obj.get("logical_source_key") or "")
        try:
            if not logical.startswith("raw/"):
                raise cr.ReceiptError(
                    "INVALID_RAW_PATH", "RFQ logical path is outside raw/")
            path_date, basename = cr._raw_rel_parts(logical[len("raw/"):])
        except cr.ReceiptError as exc:
            raise cr.ReceiptError("RFQ_TARGET_INVALID", exc.detail)
        expected_kind = ("raw_rfq_receipts"
                         if basename.startswith("rfq_receipts_")
                         else "raw_rfq")
        if (not key.startswith(prefix + "/raw/")
                or key != prefix + "/" + logical
                or cr.RFQ_RE.match(basename) is None
                or obj.get("source_kind") != expected_kind
                or obj.get("family") != "raw_durability"
                or obj.get("channel") != "rfq"
                # Receipt-time partitioning legitimately includes D+1 00/01
                # and may include still-later dependencies explicitly named
                # by seal.raw_files.  Bind to that sealed object's own path
                # date; do not rewrite it to the exchange/release date.
                or obj.get("date") != path_date
                or obj.get("seal_binding") != seal_sha
                or obj.get("evidence_binding") != "seal.raw_files"
                or obj.get("attestation_class") != "DAY_SEAL_ATTESTED"
                or obj.get("durability_scope") is not True
                or obj.get("mutable_source") is not False
                or obj.get("research_candidate") is not False
                or obj.get("research_eligible") is not False
                or obj.get("eligibility_tag_state") != "SHADOW_NOT_TAGGED"
                or obj.get("exposure_policy") != "FORBIDDEN_RFQ_DEFAULT"
                or obj.get("required") is not False
                or not crc._valid_version_id(obj.get("VersionId"))
                or obj.get("verification_state")
                != "EXACT_VERSION_FULL_SHA256"
                or obj.get("durability_verified") is not True):
            raise cr.ReceiptError(
                "RFQ_TARGET_INVALID", logical or key or "unnamed RFQ")
        try:
            if obj.get("last_modified_utc") != cr._canonical_utc(
                    obj.get("last_modified_utc"), "RFQ LastModified"):
                raise cr.ReceiptError(
                    "RFQ_TARGET_INVALID", "%s LastModified" % logical)
        except cr.ReceiptError as exc:
            if exc.code == "RFQ_TARGET_INVALID":
                raise
            raise cr.ReceiptError("RFQ_TARGET_INVALID", exc.detail)
        targets.append(obj)
    if not targets:
        raise cr.ReceiptError(
            "RFQ_SET_EMPTY", "byte receipt has no sealed RFQ objects")
    targets.sort(key=lambda obj: (
        obj["bucket"], obj["key"], obj["VersionId"]))
    return targets


def _validate_rfq_projection_row(row, *, bucket):
    if not isinstance(row, dict) or set(row) != set(RFQ_PROJECTION_FIELDS):
        raise cr.ReceiptError(
            "RFQ_ELIGIBILITY_EVIDENCE_INVALID",
            "RFQ evidence object differs from the fixed projection")
    try:
        logical = cr._safe_rel(
            row.get("logical_source_key"), "RFQ evidence logical key")
        key = cr._safe_rel(row.get("key"), "RFQ evidence key")
    except cr.ReceiptError as exc:
        raise cr.ReceiptError(
            "RFQ_ELIGIBILITY_EVIDENCE_INVALID", exc.detail)
    if (row.get("bucket") != bucket
            or not crc._valid_version_id(row.get("VersionId"))
            or not isinstance(row.get("size"), int)
            or isinstance(row.get("size"), bool) or row["size"] < 0
            or not _valid_sha256(row.get("sha256"))):
        raise cr.ReceiptError(
            "RFQ_ELIGIBILITY_EVIDENCE_INVALID", logical or key)
    try:
        modified = cr._canonical_utc(
            row.get("last_modified_utc"), "RFQ evidence LastModified")
    except cr.ReceiptError as exc:
        raise cr.ReceiptError(
            "RFQ_ELIGIBILITY_EVIDENCE_INVALID", exc.detail)
    if modified != row["last_modified_utc"]:
        raise cr.ReceiptError(
            "RFQ_ELIGIBILITY_EVIDENCE_INVALID",
            "%s LastModified is not canonical" % logical)
    return dict(row)


def _rfq_binding(evidence, evidence_sha256):
    return {
        "schema_version": RFQ_BINDING_SCHEMA,
        "state": RFQ_BINDING_STATE,
        "source": "seal.raw_files",
        "seal_sha256": evidence["seal_sha256"],
        "rfq_exact_set_sha256": evidence["rfq_exact_set_sha256"],
        "eligibility_evidence_sha256": evidence_sha256,
        "evidence_tier": "SEALED_CONFIRMATION",
        "integrity_state": "PASS",
        "quarantine_state": "CLEAR",
        "repair_branch_state": "CLOSED_NO_REPAIR",
    }


def load_rfq_eligibility_evidence(path, *, byte_receipt, bucket, prefix,
                                  now_utc):
    """Authenticate RFQ authorization and bind it to the whole exact set."""
    evidence, raw = _load_json_file(
        path, "RFQ_ELIGIBILITY_EVIDENCE_INVALID",
        limit=MAX_RFQ_ELIGIBILITY_EVIDENCE_BYTES,
        label="RFQ eligibility evidence")
    if set(evidence) != RFQ_EVIDENCE_FIELDS:
        raise cr.ReceiptError(
            "RFQ_ELIGIBILITY_EVIDENCE_INVALID",
            "evidence root differs from the fixed schema")
    fixed = {
        "schema_version": RFQ_EVIDENCE_SCHEMA,
        "state": RFQ_EVIDENCE_STATE,
        "date": byte_receipt.get("date"),
        "bucket": bucket,
        "prefix": prefix,
        "byte_receipt_set_sha256": byte_receipt.get("receipt_set_sha256"),
        "seal_sha256": (byte_receipt.get("seal") or {}).get("sha256"),
        "evidence_tier": "SEALED_CONFIRMATION",
        "integrity_state": "PASS",
        "quarantine_state": "CLEAR",
        "repair_branch_state": "CLOSED_NO_REPAIR",
    }
    for field, expected in fixed.items():
        if evidence.get(field) != expected:
            raise cr.ReceiptError(
                "RFQ_ELIGIBILITY_EVIDENCE_INVALID",
                "%s=%r, expected %r" %
                (field, evidence.get(field), expected))
    if (not isinstance(evidence.get("decider"), str)
            or not evidence["decider"].strip()
            or not _valid_sha256(evidence.get("source_evidence_sha256"))):
        raise cr.ReceiptError(
            "RFQ_ELIGIBILITY_EVIDENCE_INVALID",
            "decider/source evidence is invalid")
    try:
        decided = cr._parse_utc(
            evidence.get("decided_at_utc"), "RFQ decided_at_utc")
        now = cr._parse_utc(now_utc, "eligibility operation time")
    except cr.ReceiptError as exc:
        raise cr.ReceiptError(
            "RFQ_ELIGIBILITY_EVIDENCE_INVALID", exc.detail)
    if (evidence["decided_at_utc"] != cr._canonical_utc(
            evidence["decided_at_utc"], "RFQ decided_at_utc")
            or (decided - now).total_seconds()
            > MAX_AUDIT_FUTURE_SKEW_SECONDS):
        raise cr.ReceiptError(
            "RFQ_ELIGIBILITY_EVIDENCE_INVALID",
            "RFQ decision time is non-canonical or in the future")

    targets = select_rfq_targets(byte_receipt, prefix)
    expected_rows = _sort_rfq_projection([
        _rfq_projection(obj) for obj in targets])
    rows = evidence.get("objects")
    if not isinstance(rows, list) or not rows:
        raise cr.ReceiptError(
            "RFQ_ELIGIBILITY_EVIDENCE_INVALID", "objects is empty")
    normalized = _sort_rfq_projection([
        _validate_rfq_projection_row(row, bucket=bucket) for row in rows])
    logicals = [row["logical_source_key"] for row in normalized]
    physical = [(row["bucket"], row["key"], row["VersionId"])
                for row in normalized]
    if (len(set(logicals)) != len(logicals)
            or len(set(physical)) != len(physical)
            or normalized != expected_rows):
        raise cr.ReceiptError(
            "RFQ_ELIGIBILITY_SET_MISMATCH",
            "evidence and byte receipt RFQ exact sets differ")
    exact_sha = cr.canonical_sha256(normalized)
    if (not _valid_sha256(evidence.get("rfq_exact_set_sha256"))
            or evidence["rfq_exact_set_sha256"] != exact_sha):
        raise cr.ReceiptError(
            "RFQ_ELIGIBILITY_SET_MISMATCH", "RFQ exact-set SHA mismatch")
    evidence_sha = hashlib.sha256(raw).hexdigest()
    return evidence, evidence_sha, _rfq_binding(evidence, evidence_sha), targets


def _normalize_tags(tags, label):
    if not isinstance(tags, dict):
        raise cr.ReceiptError("TAG_PREFLIGHT_INVALID", "%s is not a map" % label)
    if len(tags) > MAX_S3_TAGS:
        raise cr.ReceiptError("TAG_LIMIT_EXCEEDED", label)
    out = {}
    for key, value in tags.items():
        if (not isinstance(key, str) or not key
                or not isinstance(value, str)):
            raise cr.ReceiptError(
                "TAG_PREFLIGHT_INVALID", "%s has an invalid tag" % label)
        if key in out:
            raise cr.ReceiptError(
                "TAG_PREFLIGHT_INVALID", "%s has duplicate tags" % label)
        out[key] = value
    return out


class AwsCliExactVersionTagger:
    """AWS CLI adapter with no versionless tagging operation."""

    def __init__(self, executable="aws"):
        self.executable = executable
        self.tag_puts = 0

    def _run(self, args, *, timeout=3600,
             error_code="S3_TAG_OPERATION_FAILED"):
        env = dict(os.environ)
        env["AWS_PAGER"] = ""
        try:
            result = subprocess.run(
                [self.executable] + args, capture_output=True, text=True,
                env=env, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise cr.ReceiptError(error_code, str(exc))
        if result.returncode:
            detail = (result.stderr or result.stdout or "").strip()[-1000:]
            raise cr.ReceiptError(
                error_code, detail or "CLI rc=%d" % result.returncode)
        if len(result.stdout.encode("utf-8")) > MAX_STS_IDENTITY_BYTES:
            raise cr.ReceiptError(
                error_code, "AWS CLI response is too large")
        try:
            payload = json.loads(
                result.stdout or "{}", object_pairs_hook=_reject_duplicate_pairs)
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise cr.ReceiptError(error_code, str(exc))
        if not isinstance(payload, dict):
            raise cr.ReceiptError(error_code, "response is not an object")
        return payload

    def get_caller_identity(self):
        payload = self._run(
            ["sts", "get-caller-identity", "--output", "json"],
            timeout=60, error_code="TAGGER_CALLER_IDENTITY_INVALID")
        if not all(field in payload for field in ("Account", "Arn", "UserId")):
            raise cr.ReceiptError(
                "TAGGER_CALLER_IDENTITY_INVALID",
                "STS get-caller-identity response is incomplete")
        return payload

    def get_tags(self, bucket, key, version_id):
        payload = self._run([
            "s3api", "get-object-tagging", "--bucket", bucket,
            "--key", key, "--version-id", version_id, "--output", "json"])
        if payload.get("VersionId") != version_id:
            raise cr.ReceiptError(
                "TAG_VERSION_MISMATCH", "%s exact GET tag mismatch" % key)
        rows = payload.get("TagSet")
        if not isinstance(rows, list) or len(rows) > MAX_S3_TAGS:
            raise cr.ReceiptError("TAG_PREFLIGHT_INVALID", key)
        tags = {}
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {"Key", "Value"}
                    or not isinstance(row.get("Key"), str)
                    or not row["Key"]
                    or not isinstance(row.get("Value"), str)
                    or row["Key"] in tags):
                raise cr.ReceiptError("TAG_PREFLIGHT_INVALID", key)
            tags[row["Key"]] = row["Value"]
        return tags

    def put_tags(self, bucket, key, version_id, tags):
        tags = _normalize_tags(tags, key)
        body = {"TagSet": [
            {"Key": name, "Value": tags[name]} for name in sorted(tags)]}
        # Keep the proof immediately adjacent to the only exact-version tag
        # mutation, including callers that bypass ``main``.
        _mutation_code_commit()
        payload = self._run([
            "s3api", "put-object-tagging", "--bucket", bucket,
            "--key", key, "--version-id", version_id,
            "--tagging", json.dumps(body, sort_keys=True,
                                     separators=(",", ":")),
            "--output", "json"])
        if payload.get("VersionId") != version_id:
            raise cr.ReceiptError(
                "TAG_VERSION_MISMATCH", "%s exact PUT tag mismatch" % key)
        self.tag_puts += 1


def _target_identity(obj):
    return obj["bucket"], obj["key"], obj["VersionId"]


def _managed_tags_for(obj, rfq_identities):
    required = {TAG_KEY: TAG_VALUE}
    if _target_identity(obj) in rfq_identities:
        required[RFQ_TAG_KEY] = RFQ_TAG_VALUE
    return required


def preflight_all_tags(targets, tagger, *, rfq_identities=frozenset()):
    """Read and validate every target before permitting the first write."""
    desired = []
    seen = set()
    for obj in targets:
        identity = _target_identity(obj)
        if identity in seen:
            raise cr.ReceiptError(
                "TAG_PREFLIGHT_INVALID", "duplicate exact-version target")
        seen.add(identity)
        label = "%s?versionId=%s" % (obj["key"], obj["VersionId"])
        current = _normalize_tags(
            tagger.get_tags(obj["bucket"], obj["key"], obj["VersionId"]),
            label)
        required = _managed_tags_for(obj, rfq_identities)
        for name, value in required.items():
            existing = current.get(name)
            if existing not in (None, value):
                raise cr.ReceiptError(
                    "ELIGIBILITY_TAG_CONFLICT",
                    "%s already has %s=%r" % (label, name, existing))
        if (RFQ_TAG_KEY not in required
                and current.get(RFQ_TAG_KEY) is not None):
            raise cr.ReceiptError(
                "ELIGIBILITY_TAG_CONFLICT",
                "%s has forbidden %s" % (label, RFQ_TAG_KEY))
        merged = dict(current)
        merged.update(required)
        if len(merged) > MAX_S3_TAGS:
            raise cr.ReceiptError("TAG_LIMIT_EXCEEDED", label)
        desired.append((obj, current, merged))
    if not set(rfq_identities).issubset(seen):
        raise cr.ReceiptError(
            "TAG_PREFLIGHT_INVALID", "RFQ tag scope leaves target set")
    return desired


def apply_and_verify_tags(preflight, tagger):
    """Apply exact-version tags serially, then re-read the complete set."""
    for obj, current, desired in preflight:
        if current != desired:
            tagger.put_tags(
                obj["bucket"], obj["key"], obj["VersionId"], desired)
    for obj, _current, desired in preflight:
        observed = _normalize_tags(
            tagger.get_tags(obj["bucket"], obj["key"], obj["VersionId"]),
            "%s?versionId=%s" % (obj["key"], obj["VersionId"]))
        if observed != desired:
            raise cr.ReceiptError(
                "TAG_READBACK_MISMATCH", obj["key"])


def verify_target_tags(targets, tagger, *, rfq_identities=frozenset()):
    """Re-read every exact target and require the eligibility tag in S3."""
    verified = []
    for obj in targets:
        label = "%s?versionId=%s" % (obj["key"], obj["VersionId"])
        observed = _normalize_tags(
            tagger.get_tags(obj["bucket"], obj["key"], obj["VersionId"]),
            label)
        required = _managed_tags_for(obj, rfq_identities)
        if any(observed.get(name) != value
               for name, value in required.items()):
            raise cr.ReceiptError(
                "DATA_TAG_REVERIFY_FAILED",
                "%s lacks required managed tags" % label)
        if (RFQ_TAG_KEY not in required
                and observed.get(RFQ_TAG_KEY) is not None):
            raise cr.ReceiptError(
                "DATA_TAG_REVERIFY_FAILED",
                "%s has forbidden %s" % (label, RFQ_TAG_KEY))
        verified.append((obj["bucket"], obj["key"], obj["VersionId"]))
    if not set(rfq_identities).issubset(set(verified)):
        raise cr.ReceiptError(
            "DATA_TAG_REVERIFY_FAILED", "RFQ tag scope leaves target set")
    return tuple(verified)


def _validate_rfq_binding(binding, *, seal_sha256, rfq_exact_set_sha256):
    if not isinstance(binding, dict) or set(binding) != RFQ_BINDING_FIELDS:
        raise cr.ReceiptError(
            "RFQ_ELIGIBILITY_BINDING_INVALID",
            "RFQ evidence binding differs from the fixed contract")
    fixed = {
        "schema_version": RFQ_BINDING_SCHEMA,
        "state": RFQ_BINDING_STATE,
        "source": "seal.raw_files",
        "seal_sha256": seal_sha256,
        "rfq_exact_set_sha256": rfq_exact_set_sha256,
        "evidence_tier": "SEALED_CONFIRMATION",
        "integrity_state": "PASS",
        "quarantine_state": "CLEAR",
        "repair_branch_state": "CLOSED_NO_REPAIR",
    }
    if any(binding.get(field) != expected
           for field, expected in fixed.items()):
        raise cr.ReceiptError(
            "RFQ_ELIGIBILITY_BINDING_INVALID",
            "RFQ evidence binding does not match its sealed exact set")
    if not _valid_sha256(binding.get("eligibility_evidence_sha256")):
        raise cr.ReceiptError(
            "RFQ_ELIGIBILITY_BINDING_INVALID",
            "RFQ evidence digest is invalid")
    return binding


def build_tagged_receipt(byte_receipt, *, audit_sha256, verified_at,
                         tagger_commit, rfq_binding=None, prefix=None):
    """Reuse byte attestations while changing only eligibility semantics."""
    cr._parse_utc(verified_at, "eligibility verified_at_utc")
    if not isinstance(tagger_commit, str) or not tagger_commit.strip():
        raise cr.ReceiptError(
            "TAGGED_RECEIPT_INVALID", "tagger code commit is missing")
    parent_sha = byte_receipt["receipt_set_sha256"]
    payload = copy.deepcopy(byte_receipt)
    rfq_identities = set()
    if rfq_binding is not None:
        if prefix is None:
            raise cr.ReceiptError(
                "RFQ_ELIGIBILITY_BINDING_INVALID",
                "canonical prefix is required for RFQ mode")
        rfq_targets = select_rfq_targets(byte_receipt, prefix)
        _validate_rfq_binding(
            rfq_binding,
            seal_sha256=byte_receipt["seal"]["sha256"],
            rfq_exact_set_sha256=rfq_exact_set_sha256(rfq_targets))
        rfq_identities = {_target_identity(obj) for obj in rfq_targets}
    for obj in payload["objects"]:
        if _target_identity(obj) in rfq_identities:
            obj["required"] = False
            obj["research_candidate"] = True
            obj["research_eligible"] = True
            obj["eligibility_tag_state"] = RFQ_DUAL_TAG_STATE
            obj["exposure_policy"] = RFQ_EXPOSURE_POLICY
            obj["evidence_binding"] = copy.deepcopy(rfq_binding)
            obj["eligibility_verified_at_utc"] = verified_at
            obj["eligibility_tagger_code_commit"] = tagger_commit
        elif obj.get("research_candidate") is True:
            obj["research_eligible"] = True
            obj["eligibility_tag_state"] = "TAGGED_VERIFIED"
            obj["exposure_policy"] = "RESEARCH_ELIGIBLE"
            obj["eligibility_verified_at_utc"] = verified_at
            obj["eligibility_tagger_code_commit"] = tagger_commit
    binding = dict(payload["seal"])
    binding["families"] = payload["families"]
    digest = cr.receipt_set_sha256(payload["date"], binding,
                                   payload["objects"])
    if digest == parent_sha:
        raise cr.ReceiptError(
            "TAGGED_RECEIPT_INVALID", "eligibility state did not change")
    payload.update({
        "receipt_set_sha256": digest,
        "durability_set_sha256": cr.scoped_object_set_sha256(
            payload["objects"], "durability_scope"),
        "research_candidate_set_sha256": cr.scoped_object_set_sha256(
            payload["objects"], "research_candidate"),
        "receipt_phase": TAGGED_PHASE,
        "byte_attestation_receipt_set_sha256": parent_sha,
        "eligibility_single_writer_audit_sha256": audit_sha256,
        "eligibility_verified_at_utc": verified_at,
        "eligibility_tagger_code_commit": tagger_commit,
        "prune_eligible": False,
    })
    return payload


def _validate_tagged_transition(byte_receipt, payload, *, audit_sha,
                                rfq_binding=None, prefix=None):
    """Prove the tagged body is exactly one allowed transition from parent."""
    try:
        expected = build_tagged_receipt(
            byte_receipt, audit_sha256=audit_sha,
            verified_at=payload.get("eligibility_verified_at_utc"),
            tagger_commit=payload.get("eligibility_tagger_code_commit"),
            rfq_binding=rfq_binding, prefix=prefix)
    except (KeyError, TypeError, cr.ReceiptError) as exc:
        detail = exc.detail if isinstance(exc, cr.ReceiptError) else str(exc)
        raise cr.ReceiptError("TAGGED_PARENT_MISMATCH", detail)
    if payload != expected:
        raise cr.ReceiptError(
            "TAGGED_PARENT_MISMATCH",
            "tagged receipt is not the exact eligibility-only parent transition")
    return payload


def _validate_tagged_receipt(payload, *, parent_sha, audit_sha, bucket,
                             prefix):
    fixed = {
        "schema_version": cr.RECEIPT_SCHEMA,
        "state": crc.DURABLE_STATE,
        "authority": crc.DURABLE_AUTHORITY,
        "authoritative": True,
        "s3_published": True,
        "prune_eligible": False,
        "receipt_phase": TAGGED_PHASE,
        "byte_attestation_receipt_set_sha256": parent_sha,
        "eligibility_single_writer_audit_sha256": audit_sha,
    }
    if (not isinstance(payload, dict)
            or any(payload.get(field) != expected
                   for field, expected in fixed.items())):
        raise cr.ReceiptError(
            "TAGGED_RECEIPT_INVALID", "authority/lineage mismatch")
    objects = payload.get("objects")
    families = payload.get("families")
    seal = payload.get("seal")
    if (not isinstance(objects, list) or not objects
            or not isinstance(families, list) or not isinstance(seal, dict)):
        raise cr.ReceiptError(
            "TAGGED_RECEIPT_INVALID", "objects/families/seal missing")
    rfq_objects = [obj for obj in objects if _is_rfq_object(obj)]
    rfq_candidates = []
    for obj in objects:
        key = str(obj.get("key") or "")
        if obj.get("bucket") != bucket or not key.startswith(prefix + "/"):
            raise cr.ReceiptError(
                "TAGGED_RECEIPT_INVALID", "object leaves canonical scope")
        if (obj.get("durability_verified") is not True
                or obj.get("verification_state")
                != "EXACT_VERSION_FULL_SHA256"
                or not crc._valid_version_id(obj.get("VersionId"))):
            raise cr.ReceiptError(
                "TAGGED_RECEIPT_INVALID", "%s lost byte attestation" % key)
        if obj.get("research_candidate") is True:
            if _is_rfq_object(obj):
                rfq_candidates.append(obj)
                if (not key.startswith(prefix + "/raw/")
                        or obj.get("research_eligible") is not True
                        or obj.get("eligibility_tag_state")
                        != RFQ_DUAL_TAG_STATE
                        or obj.get("exposure_policy") != RFQ_EXPOSURE_POLICY
                        or obj.get("required") is not False):
                    raise cr.ReceiptError(
                        "TAGGED_RECEIPT_INVALID",
                        "%s is not safely RFQ eligible" % key)
            elif (_is_raw_object(obj, prefix)
                  or obj.get("research_eligible") is not True
                  or obj.get("eligibility_tag_state") != "TAGGED_VERIFIED"
                  or obj.get("exposure_policy") != "RESEARCH_ELIGIBLE"):
                raise cr.ReceiptError(
                    "TAGGED_RECEIPT_INVALID", "%s is not safely eligible" % key)
        elif (obj.get("research_eligible") is not False
              or obj.get("eligibility_tag_state") in
              ("TAGGED_VERIFIED", RFQ_DUAL_TAG_STATE)):
            raise cr.ReceiptError(
                "TAGGED_RECEIPT_INVALID", "%s falsely claims eligibility" % key)
    if rfq_candidates:
        if ({_target_identity(obj) for obj in rfq_candidates}
                != {_target_identity(obj) for obj in rfq_objects}):
            raise cr.ReceiptError(
                "TAGGED_RECEIPT_INVALID",
                "tagged receipt does not admit the complete RFQ exact set")
        exact_sha = rfq_exact_set_sha256(rfq_candidates)
        first_binding = rfq_candidates[0].get("evidence_binding")
        _validate_rfq_binding(
            first_binding, seal_sha256=seal.get("sha256"),
            rfq_exact_set_sha256=exact_sha)
        if any(obj.get("evidence_binding") != first_binding
               for obj in rfq_candidates):
            raise cr.ReceiptError(
                "TAGGED_RECEIPT_INVALID",
                "RFQ objects do not share one evidence binding")
    binding = dict(seal)
    binding["families"] = families
    digest = cr.receipt_set_sha256(payload["date"], binding, objects)
    if (payload.get("receipt_set_sha256") != digest
            or payload.get("durability_set_sha256")
            != cr.scoped_object_set_sha256(objects, "durability_scope")
            or payload.get("research_candidate_set_sha256")
            != cr.scoped_object_set_sha256(objects, "research_candidate")):
        raise cr.ReceiptError(
            "TAGGED_RECEIPT_INVALID", "stable digest mismatch")
    return payload


def _equivalent_tagged_body(stored, expected, *, parent_sha, audit_sha,
                            bucket, prefix):
    try:
        parsed = _parse_json_object(stored, "TAGGED_RECEIPT_INVALID")
        _validate_tagged_receipt(
            parsed, parent_sha=parent_sha, audit_sha=audit_sha,
            bucket=bucket, prefix=prefix)
        expected_binding = dict(expected["seal"])
        expected_binding["families"] = expected["families"]
        parsed_binding = dict(parsed["seal"])
        parsed_binding["families"] = parsed["families"]
        return cr.stable_receipt_projection(
            parsed["date"], parsed_binding, parsed["objects"]
        ) == cr.stable_receipt_projection(
            expected["date"], expected_binding, expected["objects"])
    except cr.ReceiptError:
        return False


def publish_tagged_receipt(payload, *, byte_index, byte_receipt, audit_sha,
                           writer, tagger, output_root, bucket, prefix,
                           rfq_binding=None):
    parent_sha = payload["byte_attestation_receipt_set_sha256"]
    _validate_byte_parent(
        byte_index, byte_receipt, bucket=bucket, prefix=prefix)
    _validate_tagged_transition(
        byte_receipt, payload, audit_sha=audit_sha,
        rfq_binding=rfq_binding, prefix=prefix)
    _validate_tagged_receipt(
        payload, parent_sha=parent_sha, audit_sha=audit_sha,
        bucket=bucket, prefix=prefix)
    data_targets = select_tag_targets(byte_receipt, prefix)
    rfq_identities = set()
    if rfq_binding is not None:
        rfq_targets = select_rfq_targets(byte_receipt, prefix)
        rfq_identities = {_target_identity(obj) for obj in rfq_targets}
        data_targets.extend(rfq_targets)
        data_targets.sort(key=_target_identity)
    digest = payload["receipt_set_sha256"]
    key = cr._join_key(
        prefix, "control", "canonical-receipts", "v1",
        "date=%s" % payload["date"], "receipt-%s.json" % digest)
    body = _json_bytes(payload)
    if len(body) > crc.MAX_DURABLE_RECEIPT_BYTES:
        raise cr.ReceiptError(
            "TAGGED_RECEIPT_TOO_LARGE",
            "receipt is %d bytes; limit is %d" %
            (len(body), crc.MAX_DURABLE_RECEIPT_BYTES))

    # This re-read lives inside the publication primitive intentionally: a
    # caller cannot publish a tagged receipt merely by constructing payload.
    verify_target_tags(
        data_targets, tagger, rfq_identities=rfq_identities)

    root = pathlib.Path(output_root) / ("date=%s" % payload["date"])
    pending_path = root / ("TAGGED-PENDING-%s.json" % digest)
    pending_intent = {
        "schema_version": PENDING_INDEX_SCHEMA,
        "state": PENDING_INDEX_STATE,
        "date": payload["date"],
        "receipt_set_sha256": digest,
        "expected_receipt_object": {
            "bucket": bucket,
            "key": key,
            "size": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        },
        "complete": False,
        "completed": False,
        "prune_eligible": False,
        "receipt_phase": TAGGED_PHASE,
        "byte_attestation_receipt_set_sha256": parent_sha,
        "byte_receipt_object": copy.deepcopy(byte_index["receipt_object"]),
        "byte_receipt_payload_size": byte_index["receipt_payload_size"],
        "byte_receipt_payload_sha256":
            byte_index["receipt_payload_sha256"],
        "eligibility_single_writer_audit_sha256": audit_sha,
        "receipt_object_eligibility_tag_state": "NOT_YET_PUBLISHED",
    }
    # The intent is durable locally before the conditional remote create.  A
    # crash at any later instruction therefore leaves an unusable recovery
    # marker whose content-addressed operation can be resumed idempotently.
    cr.write_atomic_json(pending_path, pending_intent)

    binding, stored = writer.ensure_equivalent_bytes(
        bucket, key, body,
        lambda existing: _equivalent_tagged_body(
            existing, payload, parent_sha=parent_sha, audit_sha=audit_sha,
            bucket=bucket, prefix=prefix))
    crc._require_exact_binding(binding, bucket, key, stored)
    if len(stored) > crc.MAX_DURABLE_RECEIPT_BYTES:
        raise cr.ReceiptError(
            "TAGGED_RECEIPT_TOO_LARGE", "stored receipt exceeds 16 MiB")
    stored_payload = _parse_json_object(stored, "TAGGED_RECEIPT_INVALID")
    _validate_tagged_transition(
        byte_receipt, stored_payload, audit_sha=audit_sha,
        rfq_binding=rfq_binding, prefix=prefix)
    _validate_tagged_receipt(
        stored_payload, parent_sha=parent_sha, audit_sha=audit_sha,
        bucket=bucket, prefix=prefix)
    index_common = {
        "schema_version": crc.DURABLE_INDEX_SCHEMA,
        "date": payload["date"],
        "receipt_set_sha256": stored_payload["receipt_set_sha256"],
        "receipt_object": binding,
        "receipt_payload_size": len(stored),
        "receipt_payload_sha256": hashlib.sha256(stored).hexdigest(),
        "prune_eligible": False,
        "receipt_phase": TAGGED_PHASE,
        "byte_attestation_receipt_set_sha256": parent_sha,
        "byte_receipt_object": copy.deepcopy(byte_index["receipt_object"]),
        "byte_receipt_payload_size": byte_index["receipt_payload_size"],
        "byte_receipt_payload_sha256": byte_index["receipt_payload_sha256"],
        "eligibility_single_writer_audit_sha256": audit_sha,
    }
    cr.write_atomic_json(
        root / ("receipt-%s.json" % index_common["receipt_set_sha256"]),
        stored_payload)

    # The remote receipt exists at this point, but is not yet readable under
    # the W09 tag policy.  Persist an explicitly unusable recovery marker
    # before touching its tags.  A crash or tag failure leaves this artifact
    # and no complete publisher index.
    pending = copy.deepcopy(index_common)
    pending.update({
        "schema_version": PENDING_INDEX_SCHEMA,
        "state": PENDING_INDEX_STATE,
        "complete": False,
        "completed": False,
        "receipt_object_eligibility_tag_state":
            "PENDING_TAG_VERIFICATION",
    })
    cr.write_atomic_json(pending_path, pending)

    receipt_target = {
        "bucket": binding["bucket"],
        "key": binding["key"],
        "VersionId": binding["VersionId"],
    }
    receipt_preflight = preflight_all_tags([receipt_target], tagger)
    apply_and_verify_tags(receipt_preflight, tagger)

    # Re-prove the complete data set after the receipt object's own tag write;
    # a failure leaves only the pending marker, never a publisher-usable index.
    verify_target_tags(
        data_targets, tagger, rfq_identities=rfq_identities)

    # The final index is the only publisher-facing artifact.  Atomic replace
    # happens strictly after exact-VersionId tag readback succeeds.
    index = copy.deepcopy(index_common)
    index.update({
        "state": crc.DURABLE_STATE,
        "complete": True,
        "completed": True,
        "receipt_object_eligibility_tag_state": "TAGGED_VERIFIED",
    })
    path = root / ("TAGGED-DURABLE-%s.json" %
                   index["receipt_set_sha256"])
    cr.write_atomic_json(path, index)
    return path, index, stored_payload


def run_eligibility_tagging(*, receipt_index, single_writer_audit,
                            policy_evidence, reader, tagger, receipt_writer,
                            output_root, identity_provider=None,
                            expected_bucket=None, expected_prefix=None,
                            verified_at=None, tagger_commit=None,
                            include_sealed_rfq=False,
                            rfq_eligibility_evidence=None):
    """Run the fail-closed byte-receipt -> tagged-receipt transition."""
    if not isinstance(include_sealed_rfq, bool):
        raise cr.ReceiptError(
            "RFQ_OPTION_PAIR_REQUIRED", "include_sealed_rfq must be boolean")
    if bool(include_sealed_rfq) != bool(rfq_eligibility_evidence):
        raise cr.ReceiptError(
            "RFQ_OPTION_PAIR_REQUIRED",
            "--include-sealed-rfq and --rfq-eligibility-evidence "
            "must be supplied together")
    operation_time = verified_at or cr._now()
    byte_index, byte_receipt, prefix = load_durable_byte_receipt(
        receipt_index, reader, expected_bucket=expected_bucket,
        expected_prefix=expected_prefix)
    bucket = byte_index["receipt_object"]["bucket"]
    audit, audit_sha = load_single_writer_audit(
        single_writer_audit, policy_evidence,
        date=byte_receipt["date"], bucket=bucket,
        receipt_set_sha256=byte_receipt["receipt_set_sha256"],
        now_utc=operation_time, include_sealed_rfq=include_sealed_rfq)
    rfq_binding = None
    rfq_targets = []
    if include_sealed_rfq:
        _rfq_evidence, _rfq_evidence_sha, rfq_binding, rfq_targets = \
            load_rfq_eligibility_evidence(
                rfq_eligibility_evidence, byte_receipt=byte_receipt,
                bucket=bucket, prefix=prefix, now_utc=operation_time)
    provider = identity_provider or tagger
    if not callable(getattr(provider, "get_caller_identity", None)):
        raise cr.ReceiptError(
            "TAGGER_CALLER_IDENTITY_INVALID",
            "an STS get-caller-identity provider is required")
    verify_ambient_caller(audit, provider.get_caller_identity())
    targets = select_tag_targets(byte_receipt, prefix)
    rfq_identities = {_target_identity(obj) for obj in rfq_targets}
    targets.extend(rfq_targets)
    targets.sort(key=_target_identity)
    preflight = preflight_all_tags(
        targets, tagger, rfq_identities=rfq_identities)
    clean_commit = _mutation_code_commit()
    if tagger_commit is not None and tagger_commit != clean_commit:
        raise cr.ReceiptError(
            "GIT_PROVENANCE_MISMATCH",
            "requested tagger commit does not equal clean HEAD")
    apply_and_verify_tags(preflight, tagger)
    payload = build_tagged_receipt(
        byte_receipt, audit_sha256=audit_sha,
        verified_at=operation_time,
        tagger_commit=clean_commit,
        rfq_binding=rfq_binding, prefix=prefix)
    return publish_tagged_receipt(
        payload, byte_index=byte_index, byte_receipt=byte_receipt,
        audit_sha=audit_sha,
        writer=receipt_writer, tagger=tagger, output_root=output_root,
        bucket=bucket, prefix=prefix, rfq_binding=rfq_binding)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt-index", required=True)
    parser.add_argument("--single-writer-audit", required=True)
    parser.add_argument("--policy-evidence", required=True)
    parser.add_argument("--include-sealed-rfq", action="store_true")
    parser.add_argument("--rfq-eligibility-evidence")
    parser.add_argument("--bucket", default="kalshi-vault-ritcardo")
    parser.add_argument("--prefix", default="ec2")
    parser.add_argument("--aws-cli", default="aws")
    parser.add_argument("--operator-approved", action="store_true")
    parser.add_argument(
        "--output-root", default=os.path.join(
            wc.ROOT, "work", "live", "canonical_receipts", "tagged"))
    args = parser.parse_args(argv)
    if bool(args.include_sealed_rfq) != bool(args.rfq_eligibility_evidence):
        print("REFUSED: --include-sealed-rfq and "
              "--rfq-eligibility-evidence are a required pair",
              file=os.sys.stderr)
        return 2
    if not args.operator_approved:
        print("REFUSED: --operator-approved is required for exact-version "
              "tag writes", file=os.sys.stderr)
        return 2
    reader = cr.AwsCliS3Client(args.aws_cli)
    tagger = AwsCliExactVersionTagger(args.aws_cli)
    writer = crc.AwsCliConditionalWriter(args.aws_cli)
    try:
        path, index, payload = run_eligibility_tagging(
            receipt_index=os.path.abspath(args.receipt_index),
            single_writer_audit=os.path.abspath(args.single_writer_audit),
            policy_evidence=os.path.abspath(args.policy_evidence),
            include_sealed_rfq=args.include_sealed_rfq,
            rfq_eligibility_evidence=(
                os.path.abspath(args.rfq_eligibility_evidence)
                if args.rfq_eligibility_evidence else None),
            reader=reader, tagger=tagger, receipt_writer=writer,
            output_root=os.path.abspath(args.output_root),
            expected_bucket=args.bucket, expected_prefix=args.prefix)
    except cr.ReceiptError as exc:
        print("BLOCKED_INTEGRITY %s" % exc, file=os.sys.stderr)
        return 2
    print(json.dumps({
        "state": TAGGED_PHASE,
        "index": str(path),
        "receipt_set_sha256": index["receipt_set_sha256"],
        "byte_attestation_receipt_set_sha256":
            payload["byte_attestation_receipt_set_sha256"],
        "tag_puts": tagger.tag_puts,
        "receipt_puts": writer.puts,
        "receipt_reused": writer.reused,
        "prune_eligible": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
