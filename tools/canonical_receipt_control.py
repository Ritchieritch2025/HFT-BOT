#!/usr/bin/env python3
"""Gated S3 control-plane writer for forward canonical receipts.

Only three kinds of S3 writes exist here:

* small, content-addressed date controls named by a validated forward bundle;
* one content-addressed durable receipt JSON;
* one small raw-prune authority derived from an exact durable byte receipt.

The prune authority is object-scoped: ``prune_eligible=true`` covers only its
listed same-date, non-RFQ raw versions. RFQ and cross-date rows stay deferred.
Its local ``PRUNE_AUTHORITY.json`` is exposed only after conditional create and
exact-version readback of the immutable remote authority receipt.

Facts, raw, catalog, dim, copied releases, tags, lifecycle, and deletes have no
write implementation in this module.  Every put is conditional-create, must
return a non-null VersionId, and is read back by that exact VersionId before a
local success index is written.
"""
import argparse
import copy
import hashlib
import json
import os
import pathlib
import subprocess
import tempfile

import canonical_receipts as cr
import forward_canonical_receipts as fcr
import warehouse_common as wc


DURABLE_STATE = "DURABLE_RECEIPT_VERIFIED"
DURABLE_AUTHORITY = "CANONICAL_CONTROL_PLANE"
DURABLE_INDEX_SCHEMA = "canonical-durable-receipt-index-v1"
CONTROL_BINDINGS_SCHEMA = "canonical-small-control-bindings-v1"
MAX_CONTROL_OBJECT_BYTES = 64 * 1024 * 1024
MAX_CONTROL_TOTAL_BYTES = 128 * 1024 * 1024
MAX_DURABLE_RECEIPT_BYTES = 16 * 1024 * 1024
MAX_DURABLE_INDEX_BYTES = 1024 * 1024
PRUNE_AUTHORITY_SCHEMA = "canonical-raw-prune-authority-v1"
PRUNE_AUTHORITY_RECEIPT_SCHEMA = \
    "canonical-raw-prune-authority-receipt-v1"
PRUNE_AUTHORITY_STATE = "RAW_PRUNE_AUTHORIZED"
PRUNE_AUTHORITY = "CANONICAL_RAW_PRUNE_CONTROL_PLANE"
PRUNE_AUTHORITY_NAME = "PRUNE_AUTHORITY.json"
PRUNE_ELIGIBILITY_SCOPE = "LISTED_NON_RFQ_RAW_OBJECTS_ONLY"

FORWARD_AUTHORITY_FIELDS = (
    "bucket", "key", "logical_source_key", "source_kind", "family",
    "table", "channel", "date", "size", "sha256", "seal_binding",
    "evidence_binding", "attestation_class", "mutable_source", "required",
    "durability_scope", "research_candidate", "exposure_policy",
    "version_resolution", "canonical_source", "research_eligible",
    "eligibility_tag_state",
)


def _json_bytes(payload):
    return (json.dumps(payload, sort_keys=True, indent=2,
                       ensure_ascii=True).encode("utf-8") + b"\n")


def _valid_version_id(value):
    return (isinstance(value, str) and bool(value.strip())
            and value.lower() != "null")


def _require_exact_binding(binding, bucket, key, payload):
    """Reject adapters that claim success without the exact readback proof."""
    if not isinstance(binding, dict):
        raise cr.ReceiptError("CONTROL_READBACK_MISMATCH", key)
    expected_sha = hashlib.sha256(payload).hexdigest()
    if (binding.get("bucket") != bucket or binding.get("key") != key
            or binding.get("size") != len(payload)
            or binding.get("sha256") != expected_sha
            or binding.get("verification_state")
            != "EXACT_VERSION_FULL_SHA256"
            or not _valid_version_id(binding.get("VersionId"))):
        raise cr.ReceiptError("CONTROL_READBACK_MISMATCH", key)
    modified = binding.get("last_modified_utc")
    if (not isinstance(modified, str)
            or modified != cr._canonical_utc(
                modified, "%s LastModified" % key)):
        raise cr.ReceiptError("CONTROL_READBACK_MISMATCH", key)
    return binding


class AwsCliConditionalWriter:
    """The deliberately tiny real-S3 mutation surface."""

    def __init__(self, executable="aws"):
        self.executable = executable
        self.reader = cr.AwsCliS3Client(executable)
        self.puts = 0
        self.reused = 0

    def _put(self, bucket, key, path):
        # This is the sole real-S3 mutation primitive for canonical controls
        # and receipts.  Keep the provenance proof adjacent to the put so a
        # direct adapter call cannot bypass the CLI-level gate.
        cr._code_commit()
        env = dict(os.environ)
        env["AWS_PAGER"] = ""
        try:
            return subprocess.run(
                [self.executable, "s3api", "put-object",
                 "--bucket", bucket, "--key", key, "--body", path,
                 "--if-none-match", "*", "--output", "json"],
                capture_output=True, text=True, env=env, timeout=3600)
        except (OSError, subprocess.SubprocessError) as exc:
            raise cr.ReceiptError("S3_CLIENT_ERROR", str(exc))

    @staticmethod
    def _only_existing_version(history, key):
        if not isinstance(history, dict) or history.get("IsTruncated") is True:
            raise cr.ReceiptError(
                "CONTROL_HISTORY_INVALID", "version history is incomplete")
        versions = [row for row in (history.get("Versions") or [])
                    if isinstance(row, dict) and row.get("Key") == key]
        deletes = [row for row in (history.get("DeleteMarkers") or [])
                   if isinstance(row, dict) and row.get("Key") == key]
        if len(versions) != 1 or deletes:
            raise cr.ReceiptError(
                "CONTROL_CONFLICT",
                "%s has %d versions and %d delete markers" %
                (key, len(versions), len(deletes)))
        version_id = versions[0].get("VersionId")
        if not _valid_version_id(version_id):
            raise cr.ReceiptError(
                "VERSIONING_REQUIRED", "%s has null VersionId" % key)
        return version_id

    def _ensure(self, bucket, key, payload, equivalent_existing=None):
        """Return ``(binding, stored_bytes)`` after a write-once readback."""
        if not isinstance(payload, bytes):
            raise cr.ReceiptError("CONTROL_INVALID", "payload is not bytes")
        if (equivalent_existing is not None
                and len(payload) > MAX_DURABLE_RECEIPT_BYTES):
            raise cr.ReceiptError(
                "CONTROL_TOO_LARGE", "durable receipt exceeds size cap")
        fd, path = tempfile.mkstemp(prefix="canonical-control-", suffix=".bin")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            result = self._put(bucket, key, path)
            if result.returncode == 0:
                try:
                    version_id = json.loads(result.stdout or "{}").get(
                        "VersionId")
                except ValueError:
                    version_id = None
                if not _valid_version_id(version_id):
                    raise cr.ReceiptError(
                        "VERSIONING_REQUIRED",
                        "conditional put returned no VersionId for %s" % key)
                self.puts += 1
                resolved = self._only_existing_version(
                    self.reader.list_versions(bucket, key), key)
                if resolved != version_id:
                    raise cr.ReceiptError(
                        "CONTROL_CONFLICT",
                        "%s created version is not the sole version" % key)
                created = True
            else:
                detail = (result.stderr or result.stdout or "").strip()
                lower = detail.lower()
                if not any(token in lower for token in (
                        "preconditionfailed", "precondition failed", "412")):
                    raise cr.ReceiptError(
                        "S3_CONTROL_PUT_FAILED", detail[-1000:] or
                        "CLI rc=%d" % result.returncode)
                version_id = self._only_existing_version(
                    self.reader.list_versions(bucket, key), key)
                self.reused += 1
                created = False

            head = self.reader.head(bucket, key, version_id)
            if (head.get("VersionId") != version_id
                    or not isinstance(head.get("ContentLength"), int)):
                raise cr.ReceiptError(
                    "CONTROL_READBACK_MISMATCH",
                    "%s exact HEAD does not match the intended object" % key)
            content_length = head["ContentLength"]
            if ((equivalent_existing is None
                 and content_length != len(payload))
                    or (equivalent_existing is not None
                        and content_length > MAX_DURABLE_RECEIPT_BYTES)):
                raise cr.ReceiptError(
                    "CONTROL_READBACK_MISMATCH",
                    "%s exact HEAD length is outside the allowed bound" % key)
            last_modified = cr._canonical_utc(
                head.get("LastModified"), "%s LastModified" % key)
            out_dir = tempfile.mkdtemp(prefix="canonical-control-readback-")
            out = os.path.join(out_dir, "object.bin")
            try:
                self.reader.get_exact(bucket, key, version_id, out)
                if not os.path.isfile(out):
                    raise cr.ReceiptError(
                        "CONTROL_READBACK_MISMATCH",
                        "%s exact GET produced no object" % key)
                with open(out, "rb") as handle:
                    stored = handle.read()
                if (len(stored) != head["ContentLength"]
                        or (stored != payload and (
                            created or equivalent_existing is None
                            or not equivalent_existing(stored)))):
                    raise cr.ReceiptError(
                        "CONTROL_READBACK_MISMATCH",
                        "%s exact GET bytes differ" % key)
            finally:
                if os.path.isfile(out):
                    os.remove(out)
                os.rmdir(out_dir)
            final_version = self._only_existing_version(
                self.reader.list_versions(bucket, key), key)
            if final_version != version_id:
                raise cr.ReceiptError(
                    "CONTROL_CONFLICT",
                    "%s version history changed during readback" % key)
            binding = {
                "bucket": bucket,
                "key": key,
                "VersionId": version_id,
                "size": len(stored),
                "sha256": hashlib.sha256(stored).hexdigest(),
                "last_modified_utc": last_modified,
                "verification_state": "EXACT_VERSION_FULL_SHA256",
            }
            return binding, stored
        finally:
            if os.path.exists(path):
                os.remove(path)

    def ensure_bytes(self, bucket, key, payload):
        """Conditional-create bytes or safely reuse one identical version."""
        binding, _stored = self._ensure(bucket, key, payload)
        return binding

    def ensure_equivalent_bytes(self, bucket, key, payload, validator):
        """Reuse one existing immutable body only if ``validator`` accepts it."""
        if not callable(validator):
            raise cr.ReceiptError("CONTROL_INVALID", "validator is required")
        return self._ensure(
            bucket, key, payload, equivalent_existing=validator)


def _reject_duplicate_pairs(pairs):
    parsed = {}
    for key, value in pairs:
        if key in parsed:
            raise ValueError("duplicate JSON key %r" % key)
        parsed[key] = value
    return parsed


def _read_bounded_json(path, *, limit, code):
    fd = None
    try:
        fd, before = cr._open_regular_nofollow(os.fspath(path))
        if before.st_size > limit:
            raise cr.ReceiptError(code, "file exceeds %d bytes" % limit)
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, min(64 * 1024, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise cr.ReceiptError(code, "file exceeds %d bytes" % limit)
        cr._assert_fd_and_path_stable(fd, os.fspath(path), before, total)
        raw = b"".join(chunks)
    except cr.ReceiptError as exc:
        if exc.code == code:
            raise
        raise cr.ReceiptError(code, exc.detail)
    except (OSError, TypeError, ValueError) as exc:
        raise cr.ReceiptError(code, str(exc))
    finally:
        if fd is not None:
            os.close(fd)
    try:
        payload = json.loads(
            raw, object_pairs_hook=_reject_duplicate_pairs)
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise cr.ReceiptError(code, str(exc))
    if not isinstance(payload, dict):
        raise cr.ReceiptError(code, "JSON root is not an object")
    return payload, raw


def _load_exact_durable_byte_receipt(index_path, reader, *, date, bucket,
                                     prefix):
    """Authenticate one local byte index against its exact remote receipt."""
    cr._validate_date(date)
    if not isinstance(bucket, str) or not bucket or "/" in bucket:
        raise cr.ReceiptError("DURABLE_INDEX_INVALID", "invalid bucket")
    prefix = cr._safe_rel(prefix.strip("/"), "canonical prefix")
    index, _index_raw = _read_bounded_json(
        index_path, limit=MAX_DURABLE_INDEX_BYTES,
        code="DURABLE_INDEX_INVALID")
    required_index = {
        "schema_version", "state", "date", "receipt_set_sha256",
        "receipt_object", "receipt_payload_size", "receipt_payload_sha256",
        "complete", "prune_eligible",
    }
    set_sha = index.get("receipt_set_sha256")
    if (set(index) != required_index
            or index.get("schema_version") != DURABLE_INDEX_SCHEMA
            or index.get("state") != DURABLE_STATE
            or index.get("date") != date
            or index.get("complete") is not True
            or index.get("prune_eligible") is not False
            or not cr.SHA256_RE.fullmatch(str(set_sha or ""))):
        raise cr.ReceiptError(
            "DURABLE_INDEX_INVALID", "byte index authority/state is invalid")
    binding = index.get("receipt_object")
    required_binding = {
        "bucket", "key", "VersionId", "size", "sha256",
        "last_modified_utc", "verification_state",
    }
    expected_key = cr._join_key(
        prefix, "control", "canonical-receipts", "v1", "date=%s" % date,
        "receipt-%s.json" % set_sha)
    if (not isinstance(binding, dict) or set(binding) != required_binding
            or binding.get("bucket") != bucket
            or binding.get("key") != expected_key
            or not _valid_version_id(binding.get("VersionId"))
            or not isinstance(binding.get("size"), int)
            or isinstance(binding.get("size"), bool)
            or binding["size"] <= 0
            or binding["size"] > MAX_DURABLE_RECEIPT_BYTES
            or not cr.SHA256_RE.fullmatch(
                str(binding.get("sha256") or ""))
            or binding.get("verification_state")
            != "EXACT_VERSION_FULL_SHA256"
            or index.get("receipt_payload_size") != binding.get("size")
            or index.get("receipt_payload_sha256")
            != binding.get("sha256")):
        raise cr.ReceiptError(
            "DURABLE_INDEX_INVALID", "exact receipt binding is invalid")
    try:
        modified = cr._canonical_utc(
            binding.get("last_modified_utc"), "receipt LastModified")
    except cr.ReceiptError as exc:
        raise cr.ReceiptError("DURABLE_INDEX_INVALID", exc.detail)
    if modified != binding["last_modified_utc"]:
        raise cr.ReceiptError(
            "DURABLE_INDEX_INVALID", "LastModified is not canonical")
    try:
        head = reader.head(
            bucket, expected_key, binding["VersionId"])
    except Exception as exc:
        if isinstance(exc, cr.ReceiptError):
            raise
        raise cr.ReceiptError("DURABLE_RECEIPT_READ_FAILED", str(exc))
    if (not isinstance(head, dict)
            or head.get("VersionId") != binding["VersionId"]
            or head.get("ContentLength") != binding["size"]
            or cr._canonical_utc(
                head.get("LastModified"), "receipt HEAD LastModified")
            != modified):
        raise cr.ReceiptError(
            "DURABLE_RECEIPT_READ_FAILED", "exact HEAD binding mismatch")
    with tempfile.TemporaryDirectory(prefix="raw-prune-byte-receipt-") as root:
        path = os.path.join(root, "receipt.json")
        try:
            reader.get_exact(
                bucket, expected_key, binding["VersionId"], path)
            receipt, body = _read_bounded_json(
                path, limit=MAX_DURABLE_RECEIPT_BYTES,
                code="DURABLE_RECEIPT_READ_FAILED")
        except Exception as exc:
            if isinstance(exc, cr.ReceiptError):
                raise
            raise cr.ReceiptError("DURABLE_RECEIPT_READ_FAILED", str(exc))
    if (len(body) != binding["size"]
            or hashlib.sha256(body).hexdigest() != binding["sha256"]):
        raise cr.ReceiptError(
            "DURABLE_RECEIPT_READ_FAILED", "exact body size/SHA mismatch")
    required_receipt = {
        "schema_version", "state", "authority", "authoritative",
        "s3_published", "prune_eligible", "date", "receipt_set_sha256",
        "seal", "families", "durability_set_sha256",
        "research_candidate_set_sha256", "objects", "verified_at_utc",
        "publisher_code_commit",
    }
    fixed = {
        "schema_version": cr.RECEIPT_SCHEMA,
        "state": DURABLE_STATE,
        "authority": DURABLE_AUTHORITY,
        "authoritative": True,
        "s3_published": True,
        "prune_eligible": False,
        "date": date,
        "receipt_set_sha256": set_sha,
    }
    if (set(receipt) != required_receipt
            or any(receipt.get(name) != value
                   for name, value in fixed.items())):
        raise cr.ReceiptError(
            "DURABLE_RECEIPT_INVALID", "receipt authority/state is invalid")
    commit = receipt.get("publisher_code_commit")
    if (not isinstance(commit, str) or len(commit) != 40
            or any(char not in "0123456789abcdef" for char in commit)):
        raise cr.ReceiptError(
            "DURABLE_RECEIPT_INVALID", "publisher commit is not exact")
    shadow = copy.deepcopy(receipt)
    shadow.update({
        "state": "RECEIPT_VERIFIED_SHADOW",
        "authority": "SHADOW_LOCAL_ONLY",
        "authoritative": False,
        "s3_published": False,
    })
    try:
        validate_shadow_receipt(
            shadow, expected_bucket=bucket, expected_prefix=prefix)
    except cr.ReceiptError as exc:
        raise cr.ReceiptError("DURABLE_RECEIPT_INVALID", exc.detail)
    return index, receipt, prefix


def _prune_rows(receipt, bucket, prefix):
    """Project exact, same-partition, non-RFQ raw versions only."""
    date = receipt["date"]
    rows = []
    rfq_deferred = 0
    cross_date_deferred = 0
    for obj in receipt["objects"]:
        looks_raw = (obj.get("family") == "raw_durability"
                     or str(obj.get("source_kind") or "").startswith("raw_")
                     or str(obj.get("logical_source_key") or "").startswith(
                         "raw/"))
        if not looks_raw:
            continue
        if obj.get("family") != "raw_durability":
            raise cr.ReceiptError(
                "PRUNE_AUTHORITY_INVALID", "raw object has wrong family")
        key = cr._safe_rel(obj.get("key"), "raw receipt key")
        root = cr._join_key(prefix, "raw") + "/"
        if obj.get("bucket") != bucket or not key.startswith(root):
            raise cr.ReceiptError(
                "PRUNE_AUTHORITY_INVALID", "raw object leaves canonical scope")
        rel = key[len(root):]
        path_date, _basename = cr._raw_rel_parts(rel)
        kind, channel = cr._raw_classification(rel)
        if (obj.get("logical_source_key") != "raw/" + rel
                or obj.get("source_kind") != kind
                or obj.get("channel") != channel
                or obj.get("date") != path_date
                or obj.get("durability_scope") is not True
                or obj.get("durability_verified") is not True
                or obj.get("verification_state")
                != "EXACT_VERSION_FULL_SHA256"
                or not _valid_version_id(obj.get("VersionId"))
                or obj.get("mutable_source") is not False):
            raise cr.ReceiptError(
                "PRUNE_AUTHORITY_INVALID", "raw object semantics are invalid")
        if kind in {"raw_rfq", "raw_rfq_receipts"} or channel == "rfq":
            rfq_deferred += 1
            continue
        if path_date != date:
            cross_date_deferred += 1
            continue
        if (obj.get("required") is not True
                or obj.get("attestation_class") != "DAY_SEAL_ATTESTED"
                or obj.get("evidence_binding") != "seal.raw_files"
                or obj.get("exposure_policy") != "FORBIDDEN_RAW"):
            raise cr.ReceiptError(
                "PRUNE_AUTHORITY_INVALID", "raw durability proof is weak")
        rows.append({
            "local_raw_rel": rel,
            "bucket": bucket,
            "key": key,
            "VersionId": obj["VersionId"],
            "size": obj["size"],
            "sha256": obj["sha256"],
            "verification_state": "EXACT_VERSION_FULL_SHA256",
        })
    rows.sort(key=lambda row: row["local_raw_rel"])
    if not rows or len({row["local_raw_rel"] for row in rows}) != len(rows):
        raise cr.ReceiptError(
            "PRUNE_AUTHORITY_EMPTY", "no unique same-date non-RFQ raw set")
    return rows, rfq_deferred, cross_date_deferred


def _write_immutable_local_authority(path, payload):
    data = _json_bytes(payload)
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0), 0o600)
    except FileExistsError:
        existing, raw = _read_bounded_json(
            path, limit=MAX_DURABLE_RECEIPT_BYTES,
            code="PRUNE_AUTHORITY_CONFLICT")
        if existing != payload or raw != data:
            raise cr.ReceiptError(
                "PRUNE_AUTHORITY_CONFLICT", "local authority differs")
        return path
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            parent = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        except OSError:
            pass
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return path


def publish_prune_authority(date, bucket, prefix, receipt_index, reader,
                            writer, output_root):
    """Publish a small immutable authority, then expose its exact local index.

    ``prune_eligible`` applies only to the listed non-RFQ raw objects.  The
    source canonical receipt remains non-prune authority and RFQ stays
    explicitly deferred.
    """
    index, receipt, prefix = _load_exact_durable_byte_receipt(
        receipt_index, reader, date=date, bucket=bucket, prefix=prefix)
    rows, rfq_count, cross_date_count = _prune_rows(
        receipt, bucket, prefix)
    source = {
        "receipt_set_sha256": receipt["receipt_set_sha256"],
        "receipt_object": copy.deepcopy(index["receipt_object"]),
    }
    object_set_sha = cr.canonical_sha256(rows)
    stable = {
        "schema_version": PRUNE_AUTHORITY_RECEIPT_SCHEMA,
        "state": PRUNE_AUTHORITY_STATE,
        "authority": PRUNE_AUTHORITY,
        "date": date,
        "complete": True,
        "prune_eligible": True,
        "eligibility_scope": PRUNE_ELIGIBILITY_SCOPE,
        "source_canonical_receipt": source,
        "object_set_sha256": object_set_sha,
        "objects": rows,
        "rfq_state": "EXCLUDED_DEFERRED",
        "rfq_objects_deferred": rfq_count,
        "cross_date_objects_deferred": cross_date_count,
    }
    set_sha = cr.canonical_sha256(stable)
    remote_payload = copy.deepcopy(stable)
    remote_payload["receipt_set_sha256"] = set_sha
    body = _json_bytes(remote_payload)
    if len(body) > MAX_DURABLE_RECEIPT_BYTES:
        raise cr.ReceiptError(
            "PRUNE_AUTHORITY_TOO_LARGE", "authority exceeds 16 MiB")
    key = cr._join_key(
        prefix, "control", "raw-prune-authority", "v1",
        "date=%s" % date, "receipt-%s.json" % set_sha)

    local_path = pathlib.Path(output_root) / ("date=%s" % date) / \
        PRUNE_AUTHORITY_NAME
    if local_path.exists():
        existing, _raw = _read_bounded_json(
            local_path, limit=MAX_DURABLE_RECEIPT_BYTES,
            code="PRUNE_AUTHORITY_CONFLICT")
        if (existing.get("receipt_set_sha256") != set_sha
                or existing.get("objects") != rows):
            raise cr.ReceiptError(
                "PRUNE_AUTHORITY_CONFLICT", "local authority set differs")

    binding = writer.ensure_bytes(bucket, key, body)
    _require_exact_binding(binding, bucket, key, body)
    local_binding = {
        name: binding[name] for name in (
            "bucket", "key", "VersionId", "size", "sha256",
            "verification_state")
    }
    local = {
        "schema_version": PRUNE_AUTHORITY_SCHEMA,
        "state": PRUNE_AUTHORITY_STATE,
        "authority": PRUNE_AUTHORITY,
        "date": date,
        "complete": True,
        "prune_eligible": True,
        "receipt_set_sha256": set_sha,
        "receipt_object": local_binding,
        "objects": rows,
    }
    path = _write_immutable_local_authority(local_path, local)
    return path, local, remote_payload


def _load_forward_bundle(date, bucket, prefix, raw_root, warehouse_root,
                         quality_dir, aux_bundle, version_binding):
    seal, binding, *_unused = cr._authoritative_day_inputs(
        date, raw_root, warehouse_root)
    descriptor, rows = fcr.load_forward_auxiliary_set(
        aux_bundle, date, bucket, prefix, seal, binding["sha256"])
    fcr.load_forward_version_binding(
        version_binding, descriptor, rows, date, bucket, prefix)
    return descriptor, rows


def sync_small_controls(date, bucket, prefix, raw_root, warehouse_root,
                        quality_dir, aux_bundle, version_binding,
                        writer, output_root):
    """Upload only retained small-control bytes from one validated bundle."""
    _descriptor, rows = _load_forward_bundle(
        date, bucket, prefix, raw_root, warehouse_root, quality_dir,
        aux_bundle, version_binding)
    controls = [row for row in rows
                if row.get("storage_mode") == "LOCAL_FROZEN_BACKFILL"]
    if not controls:
        raise cr.ReceiptError("CONTROL_SET_EMPTY", "no small controls")
    total = sum(row["size"] for row in controls)
    if total > MAX_CONTROL_TOTAL_BYTES:
        raise cr.ReceiptError(
            "CONTROL_TOO_LARGE", "control set is %d bytes" % total)
    for row in controls:
        if row["size"] > MAX_CONTROL_OBJECT_BYTES:
            raise cr.ReceiptError(
                "CONTROL_TOO_LARGE", "%s is %d bytes" %
                (row["logical_source_key"], row["size"]))
        expected_key = fcr.expected_control_key(row, prefix, date)
        if (row["bucket"] != bucket or row["key"] != expected_key
                or not isinstance(row.get("_local_payload"), bytes)):
            raise cr.ReceiptError(
                "CONTROL_KEY_FORBIDDEN", row.get("logical_source_key"))

    bindings = []
    for row in controls:
        expected_key = fcr.expected_control_key(row, prefix, date)
        binding = writer.ensure_bytes(bucket, expected_key,
                                      row["_local_payload"])
        _require_exact_binding(
            binding, bucket, expected_key, row["_local_payload"])
        binding["logical_source_key"] = row["logical_source_key"]
        binding["source_kind"] = row["source_kind"]
        bindings.append(binding)
    bindings.sort(key=lambda row: row["logical_source_key"])
    payload = {
        "schema_version": CONTROL_BINDINGS_SCHEMA,
        "state": "CANONICAL_CONTROLS_VERIFIED",
        "date": date,
        "bucket": bucket,
        "prefix": prefix,
        "objects": bindings,
        "object_set_sha256": cr.canonical_sha256(bindings),
        "complete": True,
        "large_data_upload_bytes": 0,
    }
    path = pathlib.Path(output_root) / ("date=%s" % date) / \
        ("controls-%s.json" % payload["object_set_sha256"])
    cr.write_atomic_json(path, payload)
    return path, payload


def validate_shadow_receipt(source, expected_bucket=None,
                            expected_prefix=None):
    """Re-prove a local shadow receipt before creating a new durable payload."""
    if isinstance(source, dict):
        shadow = copy.deepcopy(source)
    else:
        try:
            payload, _fingerprint = cr._freeze_file(os.fspath(source))
            shadow = json.loads(payload)
        except (OSError, TypeError, ValueError) as exc:
            raise cr.ReceiptError("SHADOW_RECEIPT_INVALID", str(exc))
    fixed = {
        "schema_version": cr.RECEIPT_SCHEMA,
        "state": "RECEIPT_VERIFIED_SHADOW",
        "authority": "SHADOW_LOCAL_ONLY",
        "authoritative": False,
        "s3_published": False,
        "prune_eligible": False,
    }
    if any(shadow.get(key) != value for key, value in fixed.items()):
        raise cr.ReceiptError(
            "SHADOW_RECEIPT_INVALID", "shadow authority/state mismatch")
    date = shadow.get("date")
    cr._validate_date(date)
    verified_at = shadow.get("verified_at_utc")
    publisher_commit = shadow.get("publisher_code_commit")
    try:
        cr._parse_utc(verified_at, "receipt verified_at_utc")
    except cr.ReceiptError as exc:
        raise cr.ReceiptError("SHADOW_RECEIPT_INVALID", exc.detail)
    if not isinstance(publisher_commit, str) or not publisher_commit.strip():
        raise cr.ReceiptError(
            "SHADOW_RECEIPT_INVALID", "publisher code commit is missing")
    if expected_bucket is not None and (
            not isinstance(expected_bucket, str) or not expected_bucket
            or "/" in expected_bucket):
        raise cr.ReceiptError("SHADOW_RECEIPT_INVALID", "invalid bucket")
    if expected_prefix is not None:
        expected_prefix = cr._safe_rel(
            expected_prefix.strip("/"), "canonical prefix")
    objects = shadow.get("objects")
    families = shadow.get("families")
    if not isinstance(objects, list) or not objects or not isinstance(
            families, list):
        raise cr.ReceiptError(
            "SHADOW_RECEIPT_INVALID", "objects/families missing")
    if cr._family_blockers(families):
        raise cr.ReceiptError(
            "SHADOW_RECEIPT_INVALID", "required family is incomplete")
    for row in objects:
        if not isinstance(row, dict):
            raise cr.ReceiptError(
                "SHADOW_RECEIPT_INVALID", "object is not a mapping")
        try:
            key = cr._safe_rel(row.get("key"), "receipt object key")
            cr._safe_rel(
                row.get("logical_source_key"), "receipt logical key")
            cr._check_expected(
                row.get("size"), row.get("sha256"), key)
        except cr.ReceiptError as exc:
            raise cr.ReceiptError("SHADOW_RECEIPT_INVALID", exc.detail)
        if ((expected_bucket is not None
                and row.get("bucket") != expected_bucket)
                or (expected_prefix is not None
                    and not key.startswith(expected_prefix + "/"))):
            raise cr.ReceiptError(
                "SHADOW_RECEIPT_INVALID", "%s leaves receipt scope" % key)
        if (row.get("durability_verified") is not True
                or row.get("verification_state")
                != "EXACT_VERSION_FULL_SHA256"
                or not _valid_version_id(row.get("VersionId"))
                or not isinstance(row.get("last_modified_utc"), str)
                or row.get("research_eligible") is not False
                or row.get("eligibility_tag_state")
                != "SHADOW_NOT_TAGGED"
                or row.get("verified_at_utc") != verified_at
                or row.get("publisher_code_commit") != publisher_commit):
            raise cr.ReceiptError(
                "SHADOW_RECEIPT_INVALID",
                "%s is not fully exact-version verified" % row.get("key"))
        if row["last_modified_utc"] != cr._canonical_utc(
                row["last_modified_utc"], "object LastModified"):
            raise cr.ReceiptError(
                "SHADOW_RECEIPT_INVALID", "LastModified is not canonical")
    seal = dict(shadow.get("seal") or {})
    seal_rows = [row for row in objects if row.get("source_kind") == "seal"]
    if len(seal_rows) != 1:
        raise cr.ReceiptError(
            "SHADOW_RECEIPT_INVALID", "exactly one seal is required")
    seal_row = seal_rows[0]
    for field in ("bucket", "key", "VersionId", "size", "sha256"):
        if seal.get(field) != seal_row.get(field):
            raise cr.ReceiptError(
                "SHADOW_RECEIPT_INVALID", "seal object binding mismatch")
    if (expected_bucket is not None and seal.get("bucket") != expected_bucket):
        raise cr.ReceiptError(
            "SHADOW_RECEIPT_INVALID", "seal bucket mismatch")
    seal["families"] = families
    projection = cr.stable_receipt_projection(date, seal, objects)
    digest = cr.canonical_sha256(projection)
    if (shadow.get("receipt_set_sha256") != digest
            or shadow.get("durability_set_sha256")
            != cr.scoped_object_set_sha256(objects, "durability_scope")
            or shadow.get("research_candidate_set_sha256")
            != cr.scoped_object_set_sha256(objects, "research_candidate")):
        raise cr.ReceiptError(
            "SHADOW_RECEIPT_INVALID", "stable digest mismatch")
    return shadow


def validate_forward_shadow_authority(shadow, date, bucket, prefix,
                                      raw_root, warehouse_root, quality_dir,
                                      aux_bundle, version_binding):
    """Rebuild the forward whitelist and require exact two-way equality."""
    if shadow.get("date") != date:
        raise cr.ReceiptError(
            "SHADOW_RECEIPT_INVALID", "requested date differs from receipt")
    expected_seal, expected_objects = fcr.build_forward_inventory(
        date, bucket, prefix, raw_root, warehouse_root, quality_dir,
        aux_bundle, version_binding)
    expected_by_logical = {
        row["logical_source_key"]: row for row in expected_objects}
    observed_by_logical = {
        row["logical_source_key"]: row for row in shadow["objects"]}
    if (len(expected_by_logical) != len(expected_objects)
            or len(observed_by_logical) != len(shadow["objects"])
            or set(expected_by_logical) != set(observed_by_logical)):
        raise cr.ReceiptError(
            "SHADOW_RECEIPT_INVALID",
            "forward desired set and receipt set are not equal")
    for logical in sorted(expected_by_logical):
        expected = expected_by_logical[logical]
        observed = observed_by_logical[logical]
        for field in FORWARD_AUTHORITY_FIELDS:
            if observed.get(field) != expected.get(field):
                raise cr.ReceiptError(
                    "SHADOW_RECEIPT_INVALID",
                    "%s differs from forward authority at %s" %
                    (logical, field))
    if shadow.get("families") != cr._families_projection(expected_seal):
        raise cr.ReceiptError(
            "SHADOW_RECEIPT_INVALID", "family projection mismatch")
    expected_seal_projection = cr._seal_projection(expected_seal)
    for field in (
            "date", "status", "version", "method", "bucket", "key",
            "size", "sha256", "manifest_date_sha256"):
        if shadow["seal"].get(field) != expected_seal_projection.get(field):
            raise cr.ReceiptError(
                "SHADOW_RECEIPT_INVALID",
                "seal differs from forward authority at %s" % field)
    return shadow


def _equivalent_durable_body(stored_bytes, requested_shadow, bucket, prefix):
    """Accept first-writer metadata while requiring identical stable identity."""
    try:
        existing = json.loads(stored_bytes)
    except (TypeError, ValueError, UnicodeDecodeError):
        return False
    fixed = {
        "schema_version": cr.RECEIPT_SCHEMA,
        "state": DURABLE_STATE,
        "authority": DURABLE_AUTHORITY,
        "authoritative": True,
        "s3_published": True,
        "prune_eligible": False,
    }
    if (not isinstance(existing, dict)
            or any(existing.get(key) != value
                   for key, value in fixed.items())):
        return False
    candidate = copy.deepcopy(existing)
    candidate.update({
        "state": "RECEIPT_VERIFIED_SHADOW",
        "authority": "SHADOW_LOCAL_ONLY",
        "authoritative": False,
        "s3_published": False,
        "prune_eligible": False,
    })
    try:
        validated = validate_shadow_receipt(
            candidate, expected_bucket=bucket, expected_prefix=prefix)
    except cr.ReceiptError:
        return False
    requested_seal = dict(requested_shadow["seal"])
    requested_seal["families"] = requested_shadow["families"]
    existing_seal = dict(validated["seal"])
    existing_seal["families"] = validated["families"]
    return cr.stable_receipt_projection(
        validated["date"], existing_seal, validated["objects"]
    ) == cr.stable_receipt_projection(
        requested_shadow["date"], requested_seal,
        requested_shadow["objects"])


def publish_durable_receipt(date, bucket, prefix, writer, output_root, *,
                            seal_binding, verified_objects, verified_at,
                            publisher_code_commit, raw_root, warehouse_root,
                            quality_dir, aux_bundle, version_binding):
    """Publish only an in-process, complete exact-version verification.

    The private completeness markers on ``verified_objects`` are deliberately
    not serializable.  This prevents a caller from turning an editable shadow
    JSON file into canonical authority by merely recomputing its digest.
    """
    shadow_root = os.path.join(output_root, "shadow")
    shadow_path = cr.write_shadow_receipt(
        shadow_root, date, seal_binding, verified_objects, verified_at,
        publisher_code_commit)
    shadow = validate_shadow_receipt(
        shadow_path, expected_bucket=bucket, expected_prefix=prefix)
    expected_public = []
    for row in verified_objects:
        public = cr._public_object(row)
        public["verified_at_utc"] = verified_at
        public["publisher_code_commit"] = publisher_code_commit
        expected_public.append(public)
    if shadow.get("objects") != expected_public:
        raise cr.ReceiptError(
            "SHADOW_RECEIPT_INVALID",
            "disk shadow differs from in-process exact verification")
    validate_forward_shadow_authority(
        shadow, date, bucket, prefix, raw_root, warehouse_root, quality_dir,
        aux_bundle, version_binding)
    payload = copy.deepcopy(shadow)
    payload.update({
        "state": DURABLE_STATE,
        "authority": DURABLE_AUTHORITY,
        "authoritative": True,
        "s3_published": True,
        "prune_eligible": False,
    })
    digest = payload["receipt_set_sha256"]
    key = cr._join_key(
        prefix, "control", "canonical-receipts", "v1",
        "date=%s" % date, "receipt-%s.json" % digest)
    body = _json_bytes(payload)
    binding, stored_body = writer.ensure_equivalent_bytes(
        bucket, key, body,
        lambda existing: _equivalent_durable_body(
            existing, shadow, bucket, prefix))
    _require_exact_binding(binding, bucket, key, stored_body)
    if not _equivalent_durable_body(
            stored_body, shadow, bucket, prefix):
        raise cr.ReceiptError(
            "CONTROL_READBACK_MISMATCH", "durable body identity mismatch")
    try:
        stored_payload = json.loads(stored_body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise cr.ReceiptError("CONTROL_READBACK_MISMATCH", str(exc))
    index = {
        "schema_version": DURABLE_INDEX_SCHEMA,
        "state": DURABLE_STATE,
        "date": date,
        "receipt_set_sha256": digest,
        "receipt_object": binding,
        "receipt_payload_size": len(stored_body),
        "receipt_payload_sha256": hashlib.sha256(stored_body).hexdigest(),
        "complete": True,
        "prune_eligible": False,
    }
    root = pathlib.Path(output_root) / ("date=%s" % date)
    cr.write_atomic_json(
        root / ("receipt-%s.json" % digest), stored_payload)
    index_path = root / ("DURABLE-%s.json" % digest)
    cr.write_atomic_json(index_path, index)
    return index_path, index


def _source_args(parser):
    parser.add_argument("--date", required=True)
    parser.add_argument("--bucket", default="kalshi-vault-ritcardo")
    parser.add_argument("--prefix", default="ec2")
    parser.add_argument("--raw-root")
    parser.add_argument("--warehouse-root")
    parser.add_argument(
        "--quality-dir",
        default=os.path.join(wc.ROOT, "work", "event_packs"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    controls = sub.add_parser("sync-controls")
    _source_args(controls)
    controls.add_argument("--aux-bundle", required=True)
    controls.add_argument("--version-binding", required=True)
    controls.add_argument("--aws-cli", default="aws")
    controls.add_argument("--operator-approved", action="store_true")
    controls.add_argument(
        "--output-root", default=os.path.join(
            wc.ROOT, "work", "live", "canonical_receipts", "controls"))
    receipt = sub.add_parser("publish-receipt")
    _source_args(receipt)
    receipt.add_argument("--aux-bundle", required=True)
    receipt.add_argument("--version-binding", required=True)
    receipt.add_argument("--aws-cli", default="aws")
    receipt.add_argument("--operator-approved", action="store_true")
    receipt.add_argument(
        "--output-root", default=os.path.join(
            wc.ROOT, "work", "live", "canonical_receipts", "durable"))
    prune = sub.add_parser(
        "publish-prune-authority",
        help="publish an immutable non-RFQ raw prune authority")
    prune.add_argument("--date", required=True)
    prune.add_argument("--bucket", default="kalshi-vault-ritcardo")
    prune.add_argument("--prefix", default="ec2")
    prune.add_argument("--receipt-index", required=True)
    prune.add_argument("--aws-cli", default="aws")
    prune.add_argument("--operator-approved", action="store_true")
    prune.add_argument(
        "--output-root", default=os.path.join(
            wc.ROOT, "work", "live", "canonical_receipts",
            "prune-authority"))
    args = parser.parse_args(argv)
    if not args.operator_approved:
        print("REFUSED: --operator-approved is required for S3 control writes",
              file=os.sys.stderr)
        return 2
    writer = AwsCliConditionalWriter(args.aws_cli)
    try:
        if args.command == "publish-prune-authority":
            path, payload, remote = publish_prune_authority(
                args.date, args.bucket, args.prefix,
                os.path.abspath(args.receipt_index), writer.reader, writer,
                os.path.abspath(args.output_root))
            result = {
                "state": PRUNE_AUTHORITY_STATE,
                "index": str(path),
                "receipt_object": payload["receipt_object"],
                "receipt_set_sha256": payload["receipt_set_sha256"],
                "objects": len(payload["objects"]),
                "rfq_state": remote["rfq_state"],
                "s3_puts": writer.puts,
                "s3_reused": writer.reused,
                "prune_eligible": True,
            }
            print(json.dumps(result, sort_keys=True))
            return 0
        cfg = wc.load_config()
        raw_root = os.path.abspath(args.raw_root or cfg["raw_root"])
        warehouse_root = os.path.abspath(
            args.warehouse_root or cfg["warehouse_root"])
        quality_dir = os.path.abspath(args.quality_dir)
        if args.command == "publish-receipt":
            output_root = os.path.abspath(args.output_root)
            seal_binding, inventory = fcr.build_forward_inventory(
                args.date, args.bucket, args.prefix, raw_root,
                warehouse_root, quality_dir,
                os.path.abspath(args.aux_bundle),
                os.path.abspath(args.version_binding))
            verified, failures, complete = cr.verify_inventory(
                inventory, writer.reader,
                os.path.join(output_root, ".verification-tmp"))
            if not complete:
                detail = failures[0] if failures else {
                    "code": "INCOMPLETE_VERIFICATION"}
                raise cr.ReceiptError(
                    "INCOMPLETE_VERIFICATION", json.dumps(
                        detail, sort_keys=True))
            path, payload = publish_durable_receipt(
                args.date, args.bucket, args.prefix, writer, output_root,
                seal_binding=seal_binding, verified_objects=verified,
                verified_at=cr._now(),
                publisher_code_commit=cr._code_commit(),
                raw_root=raw_root, warehouse_root=warehouse_root,
                quality_dir=quality_dir,
                aux_bundle=os.path.abspath(args.aux_bundle),
                version_binding=os.path.abspath(args.version_binding))
            result = {
                "state": DURABLE_STATE,
                "index": str(path),
                "receipt_object": payload["receipt_object"],
                "s3_puts": writer.puts,
                "s3_reused": writer.reused,
                "prune_eligible": False,
            }
        else:
            path, payload = sync_small_controls(
                args.date, args.bucket, args.prefix, raw_root, warehouse_root,
                quality_dir,
                os.path.abspath(args.aux_bundle),
                os.path.abspath(args.version_binding), writer,
                os.path.abspath(args.output_root))
            result = {
                "state": payload["state"],
                "index": str(path),
                "objects": len(payload["objects"]),
                "large_data_upload_bytes": 0,
                "s3_puts": writer.puts,
                "s3_reused": writer.reused,
            }
    except cr.ReceiptError as exc:
        print("BLOCKED_INTEGRITY %s" % exc, file=os.sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
