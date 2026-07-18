#!/usr/bin/env python3
"""Build exact-version canonical S3 receipt shadows for one sealed day.

W-PUB-REF-01A phase 1 is deliberately read-only against S3.  It inventories
the objects a sealed day depends on, binds the current canonical object to a
non-null VersionId with HEAD, downloads that exact version, and verifies the
full SHA-256.  A successful run may write an immutable *local shadow* receipt;
it never writes S3, changes tags, or participates in raw pruning.

Commands:

  canonical_receipts.py freeze-aux --date YYYY-MM-DD --catalog-bindings FILE
  canonical_receipts.py plan   --date YYYY-MM-DD
  canonical_receipts.py shadow --date YYYY-MM-DD [--metadata-only]

The shadow output is intentionally branded non-authoritative.  A later,
separately audited phase must conditional-create the durable S3 receipt and
read it back before any prune-consumable local index may exist.
"""
import argparse
import collections
import concurrent.futures
import copy
import csv
import datetime
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warehouse_common as wc  # noqa: E402
import git_provenance as gp  # noqa: E402


RECEIPT_SCHEMA = "canonical-object-receipt-v1"
SHADOW_STATUS_SCHEMA = "canonical-receipt-shadow-status-v1"
AUX_SET_SCHEMA = "canonical-auxiliary-set-v1"
CATALOG_BINDINGS_SCHEMA = "canonical-catalog-cutoff-bindings-v2"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RFQ_RE = re.compile(r"^rfq(?:_receipts)?_\d{2}\.ndjson(?:\.\d+)?$")
RAW_HOUR_RE = re.compile(
    r"^(firehose|l2|rfq|rfq_receipts)_\d{2}\.ndjson(?:\.\d+)?$")
RAW_REL_RE = re.compile(
    r"^date=(\d{4}-\d{2}-\d{2})/([^/]+)$")
DIM_REQUIRED = ("series.csv", "events.csv", "markets.csv")
CATALOG_REQUIRED = (
    "series/part-00000.parquet",
    "events/part-00000.parquet",
    "markets/part-00000.parquet",
)
CATALOG_OPTIONAL = (
    "settlements/part-00000.parquet",
    "series_classified/part-00000.parquet",
)
MANIFEST_FIELDS = (
    "date", "table", "category", "subcategory", "row_count",
    "file_path", "file_md5", "created_ts",
)
CATALOG_PROVENANCE = {"VERIFIED_V2_MATCHED_CANONICAL_VERSION_HISTORY"}
FAMILY_BLOCKING_STATES = {
    "INCOMPLETE", "INVALID", "CHANGED_DURING_CUTOFF",
}
CANONICAL_RECEIPT_PROVENANCE_PATHS = (
    "tools/git_provenance.py",
    "tools/warehouse_common.py",
    "tools/publication_generation.py",
    "tools/canonical_receipts.py",
    "tools/forward_canonical_receipts.py",
    "tools/canonical_receipt_control.py",
)
MAX_NDJSON_LINE_BYTES = 64 * 1024 * 1024
MAX_DATE_CONTROL_BYTES = 64 * 1024 * 1024
MAX_AUX_DESCRIPTOR_BYTES = 16 * 1024 * 1024
MAX_VERIFY_WORKERS = 4


class ReceiptError(RuntimeError):
    """A fail-closed receipt contract violation with a stable error code."""

    def __init__(self, code, detail):
        super().__init__("%s: %s" % (code, detail))
        self.code = code
        self.detail = detail


def canonical_bytes(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def canonical_sha256(obj):
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _validate_date(date):
    if not isinstance(date, str) or not DATE_RE.match(date):
        raise ReceiptError("INVALID_DATE", "expected YYYY-MM-DD, got %r" % date)
    try:
        datetime.date.fromisoformat(date)
    except ValueError as exc:
        raise ReceiptError("INVALID_DATE", str(exc))


def _safe_rel(value, label):
    if not isinstance(value, str) or not value:
        raise ReceiptError("UNSAFE_PATH", "%s is empty or not text" % label)
    if "\\" in value or value.startswith("/") or "\x00" in value:
        raise ReceiptError("UNSAFE_PATH", "%s is not a portable relative path" % label)
    parts = value.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ReceiptError("UNSAFE_PATH", "%s escapes containment: %r" %
                           (label, value))
    return "/".join(parts)


def _join_key(*parts):
    clean = []
    for part in parts:
        if part is None:
            continue
        part = str(part).strip("/")
        if part:
            clean.append(part)
    return "/".join(clean)


def _stat_key(st):
    return (st.st_dev, st.st_ino, st.st_size,
            st.st_mtime_ns, st.st_ctime_ns)


def _open_regular_nofollow(path):
    """Open one regular file without ever following a final-component link."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise ReceiptError("LOCAL_SAFETY_UNAVAILABLE",
                           "O_NOFOLLOW is required for %s" % path)
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ReceiptError("LOCAL_MISSING", "%s: %s" % (path, exc))
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ReceiptError("LOCAL_MISSING",
                               "regular file required: %s" % path)
        return fd, opened
    except BaseException:
        os.close(fd)
        raise


def _assert_fd_and_path_stable(fd, path, before, observed_size):
    after = os.fstat(fd)
    try:
        path_after = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ReceiptError("LOCAL_SOURCE_CHANGED", "%s: %s" % (path, exc))
    if (stat.S_ISLNK(path_after.st_mode)
            or _stat_key(before) != _stat_key(after)
            or (path_after.st_dev, path_after.st_ino)
            != (after.st_dev, after.st_ino)
            or observed_size != after.st_size):
        raise ReceiptError("LOCAL_SOURCE_CHANGED",
                           "file changed while reading: %s" % path)
    return after


def _file_attestation(path):
    """Hash a regular file through one pinned fd; retain no large payload."""
    fd, before = _open_regular_nofollow(path)
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        after = _assert_fd_and_path_stable(fd, path, before, total)
    finally:
        os.close(fd)
    return after.st_size, digest.hexdigest()


def _freeze_file(path, max_bytes=None):
    """Read a small control file once from one pinned, no-follow fd."""
    fd, before = _open_regular_nofollow(path)
    chunks = []
    total = 0
    try:
        if (max_bytes is not None
                and (not isinstance(max_bytes, int) or max_bytes < 0
                     or before.st_size > max_bytes)):
            raise ReceiptError(
                "CONTROL_TOO_LARGE",
                "small control exceeds byte limit: %s" % path)
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise ReceiptError(
                    "CONTROL_TOO_LARGE",
                    "small control exceeds byte limit: %s" % path)
        after = _assert_fd_and_path_stable(fd, path, before, total)
    finally:
        os.close(fd)
    return b"".join(chunks), _stat_key(after)


class _BundleReader:
    """Pin a bundle root and resolve every child with no-follow openat."""

    def __init__(self, root):
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            raise ReceiptError("LOCAL_SAFETY_UNAVAILABLE",
                               "secure bundle traversal is unavailable")
        flags = (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                 | getattr(os, "O_CLOEXEC", 0))
        try:
            self.fd = os.open(os.fspath(root), flags)
        except OSError as exc:
            raise ReceiptError("AUX_SET_INVALID",
                               "cannot pin bundle root: %s" % exc)
        self.handles = []

    def close(self):
        for file_fd, parent_fd, _name, _rel, _fingerprint in getattr(
                self, "handles", []):
            try:
                os.close(file_fd)
            finally:
                os.close(parent_fd)
        self.handles = []
        fd = getattr(self, "fd", None)
        if fd is not None:
            os.close(fd)
            self.fd = None

    def __del__(self):
        try:
            self.close()
        except OSError:
            pass

    def read(self, rel, max_bytes=None):
        rel = _safe_rel(rel, "bundle relative path")
        parts = rel.split("/")
        current = os.dup(self.fd)
        file_fd = None
        retained = False
        try:
            dir_flags = (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                         | getattr(os, "O_CLOEXEC", 0))
            for part in parts[:-1]:
                next_fd = os.open(part, dir_flags, dir_fd=current)
                os.close(current)
                current = next_fd
            file_flags = (os.O_RDONLY | os.O_NOFOLLOW
                          | getattr(os, "O_CLOEXEC", 0))
            file_fd = os.open(parts[-1], file_flags, dir_fd=current)
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode):
                raise ReceiptError("AUX_SET_INVALID",
                                   "bundle entry is not regular: %s" % rel)
            if (max_bytes is not None
                    and (not isinstance(max_bytes, int) or max_bytes < 0
                         or before.st_size > max_bytes)):
                raise ReceiptError(
                    "AUX_SET_INVALID",
                    "bundle entry exceeds byte limit: %s" % rel)
            chunks, total = [], 0
            while True:
                chunk = os.read(file_fd, 1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise ReceiptError(
                        "AUX_SET_INVALID",
                        "bundle entry exceeds byte limit: %s" % rel)
            after = os.fstat(file_fd)
            path_after = os.stat(parts[-1], dir_fd=current,
                                 follow_symlinks=False)
            if (stat.S_ISLNK(path_after.st_mode)
                    or _stat_key(before) != _stat_key(after)
                    or (path_after.st_dev, path_after.st_ino)
                    != (after.st_dev, after.st_ino)
                    or total != after.st_size):
                raise ReceiptError("LOCAL_SOURCE_CHANGED",
                                   "bundle entry changed: %s" % rel)
            fingerprint = _stat_key(after)
            self.handles.append(
                (file_fd, current, parts[-1], rel, fingerprint))
            retained = True
            return b"".join(chunks), fingerprint
        except ReceiptError:
            raise
        except OSError as exc:
            raise ReceiptError("AUX_SET_INVALID", "%s: %s" % (rel, exc))
        finally:
            if file_fd is not None and not retained:
                os.close(file_fd)
            if not retained:
                os.close(current)

    def _stat_rel(self, rel):
        parts = rel.split("/")
        current = os.dup(self.fd)
        try:
            flags = (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                     | getattr(os, "O_CLOEXEC", 0))
            for part in parts[:-1]:
                next_fd = os.open(part, flags, dir_fd=current)
                os.close(current)
                current = next_fd
            return os.stat(parts[-1], dir_fd=current,
                           follow_symlinks=False)
        finally:
            os.close(current)

    def assert_stable(self):
        for file_fd, parent_fd, name, rel, fingerprint in self.handles:
            after = os.fstat(file_fd)
            try:
                path_after = os.stat(name, dir_fd=parent_fd,
                                     follow_symlinks=False)
                root_path_after = self._stat_rel(rel)
            except OSError as exc:
                raise ReceiptError("LOCAL_SOURCE_CHANGED",
                                   "bundle entry disappeared: %s" % exc)
            if (not stat.S_ISREG(path_after.st_mode)
                    or not stat.S_ISREG(root_path_after.st_mode)
                    or _stat_key(after) != fingerprint
                    or (path_after.st_dev, path_after.st_ino)
                    != (after.st_dev, after.st_ino)
                    or (root_path_after.st_dev, root_path_after.st_ino)
                    != (after.st_dev, after.st_ino)):
                raise ReceiptError("LOCAL_SOURCE_CHANGED",
                                   "bundle entry changed after read: %s" %
                                   name)


def _assert_fingerprint(path, expected):
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ReceiptError("LOCAL_SOURCE_CHANGED", "%s: %s" % (path, exc))
    if not stat.S_ISREG(st.st_mode):
        raise ReceiptError("LOCAL_SOURCE_CHANGED",
                           "source is no longer regular: %s" % path)
    got = (st.st_dev, st.st_ino, st.st_size,
           st.st_mtime_ns, st.st_ctime_ns)
    if got != expected:
        raise ReceiptError("LOCAL_SOURCE_CHANGED",
                           "file changed during authoritative gate: %s" % path)


def _manifest_projection_bytes(payload, date):
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReceiptError("MANIFEST_INVALID", str(exc))
    rows = [dict(row) for row in csv.DictReader(io.StringIO(text, newline=""))
            if row.get("date") == date]
    rows.sort(key=lambda row: (
        row.get("table", ""), row.get("category", ""),
        row.get("subcategory", ""), row.get("file_path", "")))
    return canonical_sha256(rows), rows


def _raw_rel_parts(rel):
    match = RAW_REL_RE.match(rel)
    if not match:
        raise ReceiptError(
            "INVALID_RAW_PATH",
            "sealed raw path must be date=YYYY-MM-DD/basename: %r" % rel)
    _validate_date(match.group(1))
    return match.group(1), match.group(2)


def _check_expected(size, digest, label):
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ReceiptError("INVALID_SEAL", "%s has invalid size %r" %
                           (label, size))
    if not isinstance(digest, str) or not SHA256_RE.match(digest):
        raise ReceiptError("INVALID_SEAL", "%s has invalid sha256 %r" %
                           (label, digest))


def load_verified_seal(warehouse_root, date):
    """Freeze and parse one full-v2 seal; authority is checked separately."""
    _validate_date(date)
    path = wc.seal_path(warehouse_root, date)
    payload, fingerprint = _freeze_file(path)
    try:
        seal = json.loads(payload)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ReceiptError("SEAL_INVALID_JSON", str(exc))
    if (seal.get("date") != date or seal.get("status") != "SEALED"
            or seal.get("version") != 2 or seal.get("method") != "full_v2"):
        raise ReceiptError(
            "SEAL_NOT_VERIFIED",
            "date/status/version/method=%r/%r/%r/%r" %
            (seal.get("date"), seal.get("status"), seal.get("version"),
             seal.get("method")))
    if not isinstance(seal.get("raw_files"), list):
        raise ReceiptError("INVALID_SEAL", "raw_files is not a list")
    if not isinstance(seal.get("archive_file_stats"), list):
        raise ReceiptError("INVALID_SEAL", "archive_file_stats is not a list")
    binding = {
        "date": date,
        "status": "SEALED",
        "version": 2,
        "method": seal.get("method"),
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "local_path": path,
        "_payload": payload,
        "_source_fingerprint": fingerprint,
    }
    return seal, binding


def _authoritative_seal_gate(date, seal_payload, manifest_payload,
                             archive_root, raw_root):
    """Run the production reader-side full-v2 verifier on frozen metadata.

    The temporary root contains exactly the already-frozen seal and manifest;
    archive bytes stay in the authoritative warehouse.  This reuses the same
    verifier as ``export_day.py --verify-seal`` without opening staging or
    invoking any network command.
    """
    try:
        import warehouse  # local production verifier; stdlib until called
        with tempfile.TemporaryDirectory(prefix="canonical-seal-gate-") as root:
            seal_dir = os.path.join(root, "seals")
            os.makedirs(seal_dir)
            with open(os.path.join(seal_dir, "date=%s.json" % date), "wb") as f:
                f.write(seal_payload)
            with open(os.path.join(root, "manifest.csv"), "wb") as f:
                f.write(manifest_payload)
            for table in warehouse.TABLES:
                warehouse._require_sealed_dates(
                    root, archive_root, raw_root, table, date, date)
    except ReceiptError:
        raise
    except Exception as exc:
        raise ReceiptError("SEAL_AUTHORITY_FAILED", str(exc))


def _raw_classification(rel):
    name = os.path.basename(rel)
    if not RAW_HOUR_RE.match(name):
        return "raw_other", "raw"
    if name.startswith("firehose_"):
        return "raw_firehose", "firehose"
    if name.startswith("l2_"):
        return "raw_l2", "orderbooks_full"
    if name.startswith("rfq_receipts_"):
        return "raw_rfq_receipts", "rfq"
    if RFQ_RE.match(name):
        return "raw_rfq", "rfq"
    return "raw_other", "raw"


def _entry(bucket, key, logical_source_key, source_kind, date, size,
           digest, seal_sha, table=None, channel=None, local_path=None,
           local_payload=None,
           evidence_binding=None, mutable_source=False, required=True,
           family=None, attestation_class="PUBLICATION_CUTOFF_FROZEN",
           durability_scope=True, research_candidate=False,
           exposure_policy="NOT_RESEARCH_EXPOSED",
           version_resolution="CURRENT_AT_CUTOFF",
           canonical_source="EXISTING_CANONICAL_SYNC",
           expected_version_id=None, expected_last_modified_utc=None):
    key = _safe_rel(key, "S3 key")
    logical_source_key = _safe_rel(logical_source_key,
                                   "logical source key")
    _check_expected(size, digest, logical_source_key)
    out = {
        "bucket": bucket,
        "key": key,
        "logical_source_key": logical_source_key,
        "source_kind": source_kind,
        "family": family or source_kind,
        "table": table,
        "channel": channel,
        "date": date,
        "size": size,
        "sha256": digest,
        "seal_binding": seal_sha,
        "evidence_binding": evidence_binding,
        "attestation_class": attestation_class,
        "mutable_source": bool(mutable_source),
        "required": bool(required),
        "durability_scope": bool(durability_scope),
        "research_candidate": bool(research_candidate),
        "exposure_policy": exposure_policy,
        "version_resolution": version_resolution,
        "canonical_source": canonical_source,
        "durability_verified": False,
        "research_eligible": False,
        "eligibility_tag_state": "SHADOW_NOT_TAGGED",
    }
    if local_path:
        out["_local_path"] = local_path
    if local_payload is not None:
        if (not isinstance(local_payload, bytes)
                or len(local_payload) != size
                or hashlib.sha256(local_payload).hexdigest() != digest):
            raise ReceiptError("LOCAL_BYTES_MISMATCH",
                               "%s retained payload mismatch" % key)
        out["_local_payload"] = local_payload
    if expected_version_id is not None:
        if (not isinstance(expected_version_id, str)
                or not expected_version_id.strip()
                or expected_version_id.lower() == "null"):
            raise ReceiptError("INVALID_VERSION_ID",
                               "%s has an empty expected VersionId" % key)
        out["_expected_version_id"] = expected_version_id
    if expected_last_modified_utc is not None:
        _parse_utc(expected_last_modified_utc, "expected_last_modified_utc")
        out["_expected_last_modified_utc"] = expected_last_modified_utc
    return out


def _add_local_file(objects, seen, *, bucket, key, logical, kind, date,
                    seal_sha, path, table=None, channel=None,
                    evidence_binding=None, mutable=False, required=True,
                    family=None, attestation_class="PUBLICATION_CUTOFF_FROZEN",
                    durability_scope=True, research_candidate=False,
                    exposure_policy="NOT_RESEARCH_EXPOSED",
                    version_resolution="CURRENT_AT_CUTOFF",
                    canonical_source="EXISTING_CANONICAL_SYNC",
                    expected_version_id=None,
                    expected_last_modified_utc=None):
    size, digest = _file_attestation(path)
    obj = _entry(bucket, key, logical, kind, date, size, digest, seal_sha,
                 table=table, channel=channel, local_path=path,
                 evidence_binding=evidence_binding,
                 mutable_source=mutable, required=required, family=family,
                 attestation_class=attestation_class,
                 durability_scope=durability_scope,
                 research_candidate=research_candidate,
                 exposure_policy=exposure_policy,
                 version_resolution=version_resolution,
                 canonical_source=canonical_source,
                 expected_version_id=expected_version_id,
                 expected_last_modified_utc=expected_last_modified_utc)
    _add_unique(objects, seen, obj)


def _add_unique(objects, seen, obj):
    identity = (obj["bucket"], obj["key"])
    logical = obj["logical_source_key"]
    if identity in seen["physical"]:
        raise ReceiptError("DUPLICATE_KEY", "duplicate canonical key %s/%s" %
                           identity)
    if logical in seen["logical"]:
        raise ReceiptError("DUPLICATE_LOGICAL_KEY",
                           "duplicate logical key %s" % logical)
    seen["physical"].add(identity)
    seen["logical"].add(logical)
    objects.append(obj)


def _add_bytes_object(objects, seen, *, bucket, key, logical, kind, date,
                      payload, seal_sha=None, **kwargs):
    obj = _entry(
        bucket, key, logical, kind, date, len(payload),
        hashlib.sha256(payload).hexdigest(), seal_sha, **kwargs)
    _add_unique(objects, seen, obj)
    return obj


def _family(name, policy, expected_basis, expected_count, observed_count,
            state, reason_code="NONE", semantic_sha256=None):
    return {
        "name": name,
        "policy": policy,
        "expected_basis": expected_basis,
        "expected_count": expected_count,
        "observed_count": observed_count,
        "state": state,
        "reason_code": reason_code,
        "semantic_sha256": semantic_sha256,
        "objects_digest": None,
    }


def _finalize_families(families, objects):
    for family in families:
        projection = [
            {"bucket": obj["bucket"], "key": obj["key"],
             "size": obj["size"], "sha256": obj["sha256"]}
            for obj in objects if obj["family"] == family["name"]
        ]
        projection.sort(key=lambda item: (item["bucket"], item["key"]))
        family["objects_digest"] = canonical_sha256(projection)
    return families


def _family_blockers(families):
    return [f for f in families if f.get("state") in FAMILY_BLOCKING_STATES]


def _validate_capture_receipt(receipt, seal, date):
    if not isinstance(receipt, dict):
        return False, "receipt is not an object"
    if receipt.get("schema_version") != "capture-gap-scan-receipt-v1":
        return False, "wrong capture receipt schema"
    if receipt.get("date") != date:
        return False, "capture receipt does not bind date"
    files = receipt.get("files")
    if not isinstance(files, list):
        return False, "capture receipt has no file inventory"
    want = {
        row["file"]: row["size"] for row in seal.get("raw_files", [])
        if row["file"].startswith("date=%s/" % date)
        and os.path.basename(row["file"]).startswith("firehose_")
    }
    got = {}
    for row in files:
        if not isinstance(row, dict) or set(("file", "bytes")) - set(row):
            return False, "capture receipt inventory entry is malformed"
        if (not isinstance(row["file"], str)
                or not isinstance(row["bytes"], int)
                or isinstance(row["bytes"], bool)
                or row["bytes"] < 0):
            return False, "capture receipt inventory values are malformed"
        if row["file"] in got:
            return False, "capture receipt inventory contains duplicates"
        got[row["file"]] = row["bytes"]
    if got != want:
        return False, "capture receipt inventory does not equal sealed firehose set"
    if (receipt.get("n_files") != len(files)
            or receipt.get("total_bytes") != sum(got.values())):
        return False, "capture receipt counts do not match inventory"
    for field in ("records", "unparsed"):
        if (not isinstance(receipt.get(field), int)
                or isinstance(receipt.get(field), bool)
                or receipt[field] < 0):
            return False, "capture receipt %s is invalid" % field
    if not isinstance(receipt.get("unreadable"), bool):
        return False, "capture receipt unreadable is not boolean"
    if receipt["unreadable"]:
        return False, "capture receipt marks the day unreadable"
    if not isinstance(receipt.get("gaps"), list):
        return False, "capture receipt gaps is not a list"
    return True, None


def _correction_source_identity(row, label):
    source_file = row.get("source_file")
    source_rel = row.get("source_raw_rel")
    if not isinstance(source_file, str) or not source_file:
        raise ReceiptError("CORRECTIONS_INVALID",
                           "%s has no source_file" % label)
    if source_rel is None:
        raise ReceiptError("CORRECTIONS_INVALID",
                           "%s has no source_raw_rel" % label)
    try:
        source_rel = _safe_rel(source_rel, "%s source_raw_rel" % label)
    except ReceiptError as exc:
        raise ReceiptError("CORRECTIONS_INVALID", exc.detail)
    match = RAW_REL_RE.match(source_rel)
    if not match or not RAW_HOUR_RE.match(match.group(2)):
        raise ReceiptError("CORRECTIONS_INVALID",
                           "%s source_raw_rel is not a raw-hour path" % label)
    _validate_correction_date(match.group(1), "%s source date" % label)
    normalized_source = source_file.replace("\\", "/")
    if not normalized_source.endswith("/" + source_rel):
        raise ReceiptError("CORRECTIONS_INVALID",
                           "%s source_file/source_raw_rel disagree" % label)
    observed = row.get("observed_at_utc")
    _parse_utc(observed, "%s observed_at_utc" % label)
    return (observed, source_file, source_rel)


def _validate_correction_date(value, label):
    try:
        _validate_date(value)
    except ReceiptError as exc:
        raise ReceiptError("CORRECTIONS_INVALID",
                           "%s: %s" % (label, exc.detail))
    return value


def _ledger_projection_lines(source_lines, date, max_output_bytes=None):
    """Project current-schema D rows from a bounded-memory line iterator."""
    selected_lines, rows = [], []
    output_size = 0
    for lineno, raw in enumerate(source_lines, 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except ValueError as exc:
            raise ReceiptError("CORRECTIONS_INVALID",
                               "ledger line %d: %s" % (lineno, exc))
        if not isinstance(row, dict):
            raise ReceiptError("CORRECTIONS_INVALID",
                               "ledger line %d is not an object" % lineno)
        if row.get("event") != "LATE_FACT_DIVERTED_TO_CORRECTIONS":
            continue
        exchange_date = row.get("exchange_date")
        legacy_date = row.get("date")
        if legacy_date is not None:
            _validate_correction_date(legacy_date,
                                      "ledger line %d date" % lineno)
        if exchange_date is None:
            if legacy_date == date:
                raise ReceiptError(
                    "CORRECTIONS_INVALID",
                    "ledger line %d uses legacy date without exchange_date" %
                    lineno)
            continue
        _validate_correction_date(
            exchange_date, "ledger line %d exchange_date" % lineno)
        if legacy_date is not None and legacy_date != exchange_date:
            raise ReceiptError(
                "CORRECTIONS_INVALID",
                "ledger line %d has conflicting date fields" % lineno)
        if exchange_date == date:
            n_rows = row.get("n_rows")
            if (not isinstance(n_rows, int) or isinstance(n_rows, bool)
                    or n_rows <= 0):
                raise ReceiptError(
                    "CORRECTIONS_INVALID",
                    "ledger line %d has invalid n_rows" % lineno)
            if row.get("seal_untouched") is not True:
                raise ReceiptError(
                    "CORRECTIONS_INVALID",
                    "ledger line %d does not attest seal_untouched" % lineno)
            _correction_source_identity(row, "ledger line %d" % lineno)
            selected = raw.strip()
            output_size += len(selected.encode("utf-8")) + 1
            if (max_output_bytes is not None
                    and output_size > max_output_bytes):
                raise ReceiptError(
                    "CORRECTIONS_INVALID",
                    "date ledger projection exceeds %d bytes" %
                    max_output_bytes)
            selected_lines.append(selected)
            rows.append(row)
    projection = (("\n".join(selected_lines) + "\n").encode("utf-8")
                  if selected_lines else b"")
    return projection, rows


def _ledger_projection(payload, date):
    """Project current-schema D rows and reject ambiguous legacy dates."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReceiptError("CORRECTIONS_INVALID", str(exc))
    return _ledger_projection_lines(text.splitlines(), date)


def _bounded_binary_lines(handle, label):
    """Yield raw NDJSON lines while bounding a malformed individual line."""
    while True:
        raw = handle.readline(MAX_NDJSON_LINE_BYTES + 1)
        if not raw:
            return
        if len(raw) > MAX_NDJSON_LINE_BYTES:
            raise ReceiptError(
                "CORRECTIONS_INVALID",
                "%s line exceeds %d bytes" %
                (label, MAX_NDJSON_LINE_BYTES))
        yield raw


def _ledger_projection_file(path, date):
    """Stream a mutable ledger through one pinned no-follow descriptor."""
    fd, before = _open_regular_nofollow(path)
    total = 0
    try:
        def text_lines():
            nonlocal total
            with os.fdopen(os.dup(fd), "rb") as handle:
                for lineno, raw in enumerate(
                        _bounded_binary_lines(handle, "ledger"), 1):
                    total += len(raw)
                    try:
                        text = raw.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise ReceiptError(
                            "CORRECTIONS_INVALID",
                            "ledger line %d: %s" % (lineno, exc))
                    yield text.rstrip("\r\n")

        projection, rows = _ledger_projection_lines(
            text_lines(), date, max_output_bytes=MAX_DATE_CONTROL_BYTES)
        after = _assert_fd_and_path_stable(fd, path, before, total)
        return projection, rows, _stat_key(after)
    finally:
        os.close(fd)


def _late_correction_key(raw, date, lineno):
    """Validate one non-empty late-row JSON line and return its source key."""
    try:
        row = json.loads(raw)
    except ValueError as exc:
        raise ReceiptError("CORRECTIONS_INVALID",
                           "late_rows line %d: %s" % (lineno, exc))
    if not isinstance(row, dict):
        raise ReceiptError("CORRECTIONS_INVALID",
                           "late_rows line %d is not an object" % lineno)
    table = row.get("table")
    values = row.get("row")
    if (table not in ("orderbooks_l1", "trades", "orderbooks_full")
            or not isinstance(values, list) or not values
            or not isinstance(values[0], int)
            or isinstance(values[0], bool)):
        raise ReceiptError(
            "CORRECTIONS_INVALID",
            "late_rows line %d has invalid table/row schema" % lineno)
    try:
        exchange_date = wc.day_of_us(values[0])
    except Exception as exc:
        raise ReceiptError(
            "CORRECTIONS_INVALID",
            "late_rows line %d has invalid exchange timestamp: %s" %
            (lineno, exc))
    if exchange_date != date:
        raise ReceiptError(
            "CORRECTIONS_INVALID",
            "late_rows line %d belongs to %s, expected %s" %
            (lineno, exchange_date, date))
    source = _correction_source_identity(
        row, "late_rows line %d" % lineno)
    start = row.get("source_start_offset")
    end = row.get("source_end_offset")
    if ((start is None) != (end is None)
            or start is None
            or not isinstance(start, int) or isinstance(start, bool)
            or not isinstance(end, int) or isinstance(end, bool)
            or start < 0 or end < start):
        raise ReceiptError(
            "CORRECTIONS_INVALID",
            "late_rows line %d has invalid source offsets" % lineno)
    return date, source


def _late_correction_counts(payload, date):
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReceiptError("CORRECTIONS_INVALID", "late_rows: %s" % exc)
    counts = collections.Counter()
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        counts[_late_correction_key(raw, date, lineno)] += 1
    return counts


def _correction_counts_projection(date, counts):
    rows = [{
        "exchange_date": date,
        "observed_at_utc": key[1][0],
        "source_file": key[1][1],
        "source_raw_rel": key[1][2],
        "n_rows": value,
    } for key, value in counts.items()]
    rows.sort(key=lambda row: (
        row["observed_at_utc"], row["source_file"], row["source_raw_rel"]))
    return rows


def _correction_validation_from_ledger(ledger_rows, date):
    counts = collections.Counter()
    for index, row in enumerate(ledger_rows, 1):
        source = _correction_source_identity(
            row, "date ledger row %d" % index)
        counts[(date, source)] += row["n_rows"]
    return counts, {
        "schema_version": "late-correction-stream-validation-v1",
        "row_count": sum(counts.values()),
        "source_counts_sha256": canonical_sha256(
            _correction_counts_projection(date, counts)),
    }


def _late_correction_file_attestation(path, ledger_rows, date):
    """Validate and hash large late-row bytes without retaining the payload."""
    ledger_counts, expected_validation = _correction_validation_from_ledger(
        ledger_rows, date)
    remaining = ledger_counts.copy()
    digest = hashlib.sha256()
    total = row_count = 0
    fd, before = _open_regular_nofollow(path)
    try:
        with os.fdopen(os.dup(fd), "rb") as handle:
            for lineno, raw in enumerate(
                    _bounded_binary_lines(handle, "late_rows"), 1):
                total += len(raw)
                digest.update(raw)
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ReceiptError(
                        "CORRECTIONS_INVALID",
                        "late_rows line %d: %s" % (lineno, exc))
                if not text.strip():
                    continue
                key = _late_correction_key(
                    text.rstrip("\r\n"), date, lineno)
                if remaining.get(key, 0) <= 0:
                    raise ReceiptError(
                        "CORRECTIONS_INVALID",
                        "late_rows and ledger source/date groups do not agree")
                remaining[key] -= 1
                row_count += 1
        after = _assert_fd_and_path_stable(fd, path, before, total)
    finally:
        os.close(fd)
    if any(remaining.values()):
        raise ReceiptError(
            "CORRECTIONS_INVALID",
            "late_rows and ledger source/date groups do not agree")
    validation = dict(expected_validation, row_count=row_count)
    return after.st_size, digest.hexdigest(), validation, _stat_key(after)


def _validate_correction_pair(late_payload, ledger_rows, date):
    late_counts = _late_correction_counts(late_payload, date)
    ledger_counts = collections.Counter()
    for index, row in enumerate(ledger_rows, 1):
        source = _correction_source_identity(
            row, "date ledger row %d" % index)
        ledger_counts[(date, source)] += row["n_rows"]
    if late_counts != ledger_counts:
        raise ReceiptError(
            "CORRECTIONS_INVALID",
            "late_rows and ledger source/date groups do not agree")
    return late_counts


def _parse_utc(value, label):
    if not isinstance(value, str) or not value:
        raise ReceiptError("INVALID_CUTOFF", "%s is missing" % label)
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReceiptError("INVALID_CUTOFF", "%s: %s" % (label, exc))
    if parsed.tzinfo is None:
        raise ReceiptError("INVALID_CUTOFF", "%s is not timezone-aware" % label)
    return parsed.astimezone(datetime.timezone.utc)


def _canonical_utc(value, label):
    """Normalize an observed RFC3339 timestamp for stable receipt identity."""
    parsed = _parse_utc(value, label)
    timespec = "microseconds" if parsed.microsecond else "seconds"
    return parsed.isoformat(timespec=timespec).replace("+00:00", "Z")


def _authoritative_day_inputs(date, raw_root, warehouse_root):
    """Freeze and verify the seal plus the full manifest's date projection."""
    seal, binding = load_verified_seal(warehouse_root, date)
    raw_proofs = seal["raw_files"]
    fact_proofs = seal["archive_file_stats"]

    for label, proofs in (("raw", raw_proofs), ("archive", fact_proofs)):
        for proof in proofs:
            if not isinstance(proof, dict):
                raise ReceiptError("INVALID_SEAL",
                                   "%s proof is not an object" % label)
            rel = _safe_rel(proof.get("file"), "seal %s file" % label)
            _check_expected(proof.get("size"), proof.get("sha256"), rel)
            if label == "raw":
                _raw_rel_parts(rel)
            elif proof.get("table") not in (
                    "orderbooks_l1", "orderbooks_full", "trades"):
                raise ReceiptError("INVALID_SEAL",
                                   "unknown archive table %r" %
                                   proof.get("table"))

    manifest_path = os.path.join(warehouse_root, "manifest.csv")
    manifest_payload, manifest_fingerprint = _freeze_file(manifest_path)
    manifest_digest, manifest_rows = _manifest_projection_bytes(
        manifest_payload, date)
    if manifest_digest != seal.get("manifest_date_sha256"):
        raise ReceiptError(
            "MANIFEST_DATE_PROJECTION_MISMATCH",
            "frozen manifest=%s seal=%s" %
            (manifest_digest, seal.get("manifest_date_sha256")))
    if seal.get("archive_files") != len(fact_proofs):
        raise ReceiptError("INVALID_SEAL", "archive_files count mismatch")
    try:
        row_count = sum(int(row.get("row_count") or 0)
                        for row in manifest_rows)
    except (TypeError, ValueError) as exc:
        raise ReceiptError("MANIFEST_INVALID", str(exc))
    if seal.get("archive_rows") != row_count:
        raise ReceiptError("INVALID_SEAL", "archive_rows count mismatch")

    _authoritative_seal_gate(
        date, binding["_payload"], manifest_payload,
        os.path.join(warehouse_root, "facts"), raw_root)
    _assert_fingerprint(binding["local_path"],
                        binding["_source_fingerprint"])
    _assert_fingerprint(manifest_path, manifest_fingerprint)
    return (seal, binding, manifest_payload, manifest_digest,
            manifest_rows, raw_proofs, fact_proofs)


def _manifest_day_csv(payload, date):
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReceiptError("MANIFEST_INVALID", str(exc))
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != MANIFEST_FIELDS:
        raise ReceiptError("MANIFEST_INVALID",
                           "unexpected manifest fields %r" % reader.fieldnames)
    rows = [dict(row) for row in reader if row.get("date") == date]
    rows.sort(key=lambda row: (
        row.get("table", ""), row.get("category", ""),
        row.get("subcategory", ""), row.get("file_path", "")))
    for row in rows:
        _safe_rel(row.get("file_path"), "manifest file_path")
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=MANIFEST_FIELDS,
                            lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode("utf-8"), canonical_sha256(rows), rows


def _load_json_source(value, label):
    if isinstance(value, dict):
        return copy.deepcopy(value)
    try:
        payload, _fingerprint = _freeze_file(os.fspath(value))
        loaded = json.loads(payload)
    except (OSError, TypeError, ValueError) as exc:
        raise ReceiptError("%s_INVALID" % label, str(exc))
    if not isinstance(loaded, dict):
        raise ReceiptError("%s_INVALID" % label, "root is not an object")
    return loaded


def _validate_v2_release_manifest(payload, date, release_id, seal_sha):
    try:
        manifest = json.loads(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReceiptError("CATALOG_BINDINGS_INVALID",
                           "source v2 manifest: %s" % exc)
    if not isinstance(manifest, dict):
        raise ReceiptError("CATALOG_BINDINGS_INVALID",
                           "source v2 manifest is not an object")
    if (manifest.get("schema_version") != "research-release-manifest-v2"
            or manifest.get("date") != date
            or manifest.get("release_id") != release_id):
        raise ReceiptError("CATALOG_BINDINGS_INVALID",
                           "source v2 manifest identity mismatch")
    state = manifest.get("publication_state")
    state_sha = manifest.get("publication_state_sha256")
    if (not isinstance(state, dict) or not isinstance(state_sha, str)
            or canonical_sha256(state) != state_sha):
        raise ReceiptError("CATALOG_BINDINGS_INVALID",
                           "source v2 publication-state digest mismatch")
    seal = manifest.get("seal")
    if not isinstance(seal, dict) or seal.get("sha256") != seal_sha:
        raise ReceiptError("CATALOG_BINDINGS_INVALID",
                           "source v2 seal binding mismatch")
    expected_release_id = "%s__seal-%s__pub-%s" % (
        date, seal_sha[:8], state_sha[:16])
    if release_id != expected_release_id:
        raise ReceiptError("CATALOG_BINDINGS_INVALID",
                           "source v2 release_id digest mismatch")

    cutoff_text = manifest.get("generated_at_utc")
    cutoff = _parse_utc(cutoff_text, "source v2 generated_at_utc")
    corrections = manifest.get("corrections")
    if (not isinstance(corrections, dict)
            or corrections.get("cutoff_utc") != cutoff_text):
        raise ReceiptError("CATALOG_BINDINGS_INVALID",
                           "source v2 cutoff fields disagree")

    objects = manifest.get("objects")
    if not isinstance(objects, list) or not objects:
        raise ReceiptError("CATALOG_BINDINGS_INVALID",
                           "source v2 object inventory is missing")
    by_key = {}
    version_bindings = {}
    for index, row in enumerate(objects):
        if not isinstance(row, dict):
            raise ReceiptError("CATALOG_BINDINGS_INVALID",
                               "source v2 object %d is malformed" % index)
        key = _safe_rel(row.get("key"), "source v2 object key")
        if key in by_key:
            raise ReceiptError("CATALOG_BINDINGS_INVALID",
                               "source v2 object key is duplicated")
        _check_expected(row.get("size"), row.get("sha256"), key)
        version_id = row.get("version_id")
        if (not isinstance(version_id, str) or not version_id.strip()
                or version_id.lower() == "null"):
            raise ReceiptError("CATALOG_BINDINGS_INVALID",
                               "source v2 object has no VersionId: %s" % key)
        by_key[key] = row
        version_bindings[key] = version_id
    version_binding = manifest.get("version_binding")
    if (not isinstance(version_binding, dict)
            or version_binding.get("mode") != "VERSION_BOUND"
            or version_binding.get("bindings_sha256")
            != canonical_sha256(version_bindings)):
        raise ReceiptError("CATALOG_BINDINGS_INVALID",
                           "source v2 VersionId binding is invalid")
    post_verify = manifest.get("post_upload_verification")
    if (not isinstance(post_verify, dict)
            or post_verify.get("objects_verified") != len(objects)):
        raise ReceiptError("CATALOG_BINDINGS_INVALID",
                           "source v2 verification count is invalid")

    catalog = {}
    marker = "catalog/"
    allowed = set(CATALOG_REQUIRED + CATALOG_OPTIONAL)
    for key, row in by_key.items():
        if not key.startswith(marker):
            continue
        rel = key[len(marker):]
        if rel not in allowed:
            raise ReceiptError("CATALOG_BINDINGS_INVALID",
                               "unexpected source v2 catalog path %s" % rel)
        catalog[rel] = {"size": row["size"], "sha256": row["sha256"]}
    missing = set(CATALOG_REQUIRED) - set(catalog)
    if missing:
        raise ReceiptError("CATALOG_BINDINGS_INVALID",
                           "source v2 catalog objects missing: %s" %
                           sorted(missing))
    evidence_identity = canonical_sha256({
        "schema_version": manifest["schema_version"],
        "release_id": release_id,
        "date": date,
        "generated_at_utc": cutoff_text,
        "corrections_cutoff_utc": corrections["cutoff_utc"],
        "publication_state_sha256": state_sha,
        "seal_sha256": seal_sha,
        "objects": sorted([{
            "key": row["key"], "size": row["size"],
            "sha256": row["sha256"], "version_id": row["version_id"],
        } for row in objects], key=lambda row: row["key"]),
        "version_binding": {
            "mode": version_binding["mode"],
            "bindings_sha256": version_binding["bindings_sha256"],
        },
        "objects_verified": post_verify["objects_verified"],
    })
    return manifest, cutoff_text, cutoff, catalog, evidence_identity


def _normalize_catalog_bindings(source, date, bucket, prefix, seal_sha):
    bindings = _load_json_source(source, "CATALOG_BINDINGS")
    if set(bindings) != {
            "schema_version", "date", "bucket", "prefix", "provenance",
            "source_release", "objects"}:
        raise ReceiptError("CATALOG_BINDINGS_INVALID",
                           "unexpected top-level binding fields")
    if bindings.get("schema_version") != CATALOG_BINDINGS_SCHEMA:
        raise ReceiptError("CATALOG_BINDINGS_INVALID", "wrong schema_version")
    if bindings.get("date") != date:
        raise ReceiptError("CATALOG_BINDINGS_INVALID", "date mismatch")
    if bindings.get("bucket") != bucket or bindings.get("prefix") != prefix:
        raise ReceiptError("CATALOG_BINDINGS_INVALID",
                           "bucket/prefix mismatch")
    provenance = bindings.get("provenance")
    if provenance not in CATALOG_PROVENANCE:
        raise ReceiptError("CATALOG_BINDINGS_INVALID",
                           "unsupported provenance %r" % provenance)
    source_release = bindings.get("source_release")
    if not isinstance(source_release, dict):
        raise ReceiptError("CATALOG_BINDINGS_INVALID",
                           "source_release is missing")
    if set(source_release) != {
            "release_id", "bucket", "key", "VersionId", "size",
            "sha256", "last_modified_utc", "local_path"}:
        raise ReceiptError("CATALOG_BINDINGS_INVALID",
                           "unexpected source_release fields")
    source_release_id = _safe_rel(
        source_release.get("release_id"), "source release_id")
    source_bucket = source_release.get("bucket")
    if source_bucket != bucket:
        raise ReceiptError("CATALOG_BINDINGS_INVALID",
                           "source release bucket is not the trusted bucket")
    source_key = _safe_rel(source_release.get("key"),
                           "source release manifest key")
    expected_source_key = _join_key(
        "research", "releases", source_release_id, "MANIFEST.json")
    if source_key != expected_source_key:
        raise ReceiptError("CATALOG_BINDINGS_INVALID",
                           "source release manifest key mismatch")
    _check_expected(source_release.get("size"),
                    source_release.get("sha256"), source_key)
    source_version = source_release.get("VersionId")
    if (not isinstance(source_version, str) or not source_version.strip()
            or source_version.lower() == "null"):
        raise ReceiptError("CATALOG_BINDINGS_INVALID",
                           "source release manifest VersionId is empty")
    source_modified_text = source_release.get("last_modified_utc")
    source_modified = _parse_utc(
        source_modified_text, "source release manifest LastModified")
    source_path = source_release.get("local_path")
    if not isinstance(source_path, str) or not source_path:
        raise ReceiptError("CATALOG_BINDINGS_INVALID",
                           "source release local manifest is missing")
    source_payload, _source_fp = _freeze_file(source_path)
    if (len(source_payload) != source_release["size"]
            or hashlib.sha256(source_payload).hexdigest()
            != source_release["sha256"]):
        raise ReceiptError("CATALOG_BINDINGS_INVALID",
                           "source release local manifest bytes mismatch")
    (_source_manifest, generated_text, generated_at,
     source_catalog, evidence_identity) = _validate_v2_release_manifest(
         source_payload, date, source_release_id, seal_sha)
    if source_modified < generated_at:
        raise ReceiptError("CATALOG_BINDINGS_INVALID",
                           "source manifest predates its generated time")
    cutoff_text = source_modified_text
    cutoff = source_modified

    normalized, seen = [], set()
    for raw in bindings.get("objects") or []:
        if not isinstance(raw, dict):
            raise ReceiptError("CATALOG_BINDINGS_INVALID",
                               "catalog entry is not an object")
        if set(raw) != {
                "bucket", "key", "logical_source_key", "VersionId",
                "size", "sha256", "last_modified_utc"}:
            raise ReceiptError("CATALOG_BINDINGS_INVALID",
                               "unexpected catalog entry fields")
        logical = _safe_rel(raw.get("logical_source_key"),
                            "catalog logical_source_key")
        marker = "warehouse/catalog/"
        if not logical.startswith(marker):
            raise ReceiptError("CATALOG_BINDINGS_INVALID",
                               "unexpected logical key %s" % logical)
        rel = logical[len(marker):]
        if rel not in CATALOG_REQUIRED + CATALOG_OPTIONAL or rel in seen:
            raise ReceiptError("CATALOG_BINDINGS_INVALID",
                               "unexpected or duplicate catalog path %s" % rel)
        seen.add(rel)
        key = _safe_rel(raw.get("key"), "catalog S3 key")
        expected_key = _join_key(prefix, "warehouse", "catalog", rel)
        if raw.get("bucket") != bucket or key != expected_key:
            raise ReceiptError("CATALOG_BINDINGS_INVALID",
                               "catalog bucket/key mismatch for %s" % rel)
        _check_expected(raw.get("size"), raw.get("sha256"), logical)
        version_id = raw.get("VersionId")
        if (not isinstance(version_id, str) or not version_id.strip()
                or version_id.lower() == "null"):
            raise ReceiptError("CATALOG_BINDINGS_INVALID",
                               "catalog VersionId is empty for %s" % rel)
        last_modified = _parse_utc(raw.get("last_modified_utc"),
                                   "%s last_modified_utc" % rel)
        if last_modified > cutoff:
            raise ReceiptError("CATALOG_BINDINGS_INVALID",
                               "%s is newer than cutoff" % rel)
        normalized.append({
            "bucket": bucket,
            "key": key,
            "logical_source_key": logical,
            "rel": rel,
            "VersionId": version_id,
            "size": raw["size"],
            "sha256": raw["sha256"],
            "last_modified_utc": raw["last_modified_utc"],
        })
    if seen != set(source_catalog):
        raise ReceiptError("CATALOG_BINDINGS_INVALID",
                           "canonical/v2 catalog sets differ: canonical=%s v2=%s" %
                           (sorted(seen), sorted(source_catalog)))
    for row in normalized:
        evidence = source_catalog[row["rel"]]
        if (row["size"] != evidence["size"]
                or row["sha256"] != evidence["sha256"]):
            raise ReceiptError(
                "CATALOG_BINDINGS_INVALID",
                "canonical/v2 catalog bytes differ for %s" % row["rel"])
    normalized.sort(key=lambda row: row["logical_source_key"])
    catalog_set_sha = canonical_sha256([{
        "bucket": row["bucket"], "key": row["key"],
        "logical_source_key": row["logical_source_key"],
        "VersionId": row["VersionId"], "size": row["size"],
        "sha256": row["sha256"],
    } for row in normalized])
    return {
        "cutoff_utc": cutoff_text,
        "provenance": provenance,
        "source_release_id": source_release_id,
        "source_release": {
            "bucket": source_bucket,
            "key": source_key,
            "VersionId": source_version,
            "size": source_release["size"],
            "sha256": source_release["sha256"],
            "last_modified_utc": source_modified_text,
            "generated_at_utc": generated_text,
            "evidence_identity_sha256": evidence_identity,
            "payload": source_payload,
        },
        "catalog_set_sha256": catalog_set_sha,
        "objects": normalized,
    }


def _stage_payload(root, rel, payload):
    rel = _safe_rel(rel, "auxiliary local path")
    path = pathlib.Path(root, *rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as f:
        f.write(payload)
    return str(path), len(payload), hashlib.sha256(payload).hexdigest()


def _aux_local_object(root, rel, payload, **metadata):
    _path, size, digest = _stage_payload(root, rel, payload)
    out = {
        "storage_mode": "LOCAL_FROZEN_BACKFILL",
        "local_relpath": rel,
        "size": size,
        "sha256": digest,
        "expected_version_id": None,
    }
    out.update(metadata)
    return out


def _aux_existing_witness(root, rel, payload, version_id, **metadata):
    _path, size, digest = _stage_payload(root, rel, payload)
    out = {
        "storage_mode": "EXISTING_EXACT_VERSION_WITH_LOCAL_WITNESS",
        "local_relpath": rel,
        "size": size,
        "sha256": digest,
        "expected_version_id": version_id,
    }
    out.update(metadata)
    return out


def freeze_auxiliary_set(date, bucket, prefix, raw_root, warehouse_root,
                         quality_dir, aux_root, catalog_bindings):
    """Freeze only small date-D controls plus exact existing catalog refs."""
    _validate_date(date)
    prefix = _safe_rel(prefix.strip("/"), "canonical prefix")
    (seal, binding, manifest_payload, manifest_digest, _manifest_rows,
     _raw_proofs, fact_proofs) = _authoritative_day_inputs(
         date, raw_root, warehouse_root)
    seal_sha = binding["sha256"]
    catalog = _normalize_catalog_bindings(
        catalog_bindings, date, bucket, prefix, seal_sha)

    date_root = pathlib.Path(aux_root) / ("date=%s" % date)
    date_root.mkdir(parents=True, exist_ok=True)
    pending = tempfile.mkdtemp(prefix=".pending-", dir=str(date_root))
    objects = []
    try:
        day_manifest, day_digest, _day_rows = _manifest_day_csv(
            manifest_payload, date)
        if day_digest != manifest_digest:
            raise ReceiptError("MANIFEST_DATE_PROJECTION_MISMATCH",
                               "serialized day projection changed semantics")
        manifest_sha = hashlib.sha256(day_manifest).hexdigest()
        objects.append(_aux_local_object(
            pending, "manifest/manifest.csv", day_manifest,
            bucket=bucket,
            key=_join_key(
                prefix, "warehouse", "publication-snapshots", "v1",
                "date=%s" % date, "manifest", "sha256=%s" % manifest_sha,
                "manifest.csv"),
            logical_source_key="warehouse/manifest.csv",
            source_kind="warehouse_manifest_day",
            family="manifest_date_projection", table=None, channel=None,
            date=date, seal_binding=seal_sha,
            evidence_binding={"manifest_date_sha256": manifest_digest},
            research_candidate=True,
            exposure_policy="PENDING_ELIGIBILITY_TAG",
            version_resolution="WRITE_ONCE_CONTENT_ADDRESSED_EXACT",
            canonical_source="PLANNED_DATE_SCOPED_CONTROL_SYNC"))

        source_release = catalog["source_release"]
        objects.append(_aux_existing_witness(
            pending, "catalog-evidence/source-v2-manifest.json",
            source_release["payload"], source_release["VersionId"],
            bucket=source_release["bucket"], key=source_release["key"],
            logical_source_key=_join_key(
                "research", "releases", catalog["source_release_id"],
                "MANIFEST.json"),
            source_kind="source_v2_manifest",
            family="catalog_cutoff_evidence", table=None, channel=None,
            date=date, seal_binding=seal_sha,
            source_last_modified_utc=source_release["last_modified_utc"],
            evidence_binding={
                "release_id": catalog["source_release_id"],
                "cutoff_utc": catalog["cutoff_utc"],
                "generated_at_utc": source_release["generated_at_utc"],
                "evidence_identity_sha256":
                    source_release["evidence_identity_sha256"],
            },
            research_candidate=False,
            exposure_policy="NOT_RESEARCH_EXPOSED",
            version_resolution="EXACT_VERSION_EVIDENCE",
            canonical_source="EXISTING_IMMUTABLE_V2_MANIFEST"))

        catalog_evidence = {
            "catalog_set_sha256": catalog["catalog_set_sha256"],
            "cutoff_utc": catalog["cutoff_utc"],
            "provenance": catalog["provenance"],
            "source_release_id": catalog["source_release_id"],
            "source_manifest_bucket": source_release["bucket"],
            "source_manifest_key": source_release["key"],
            "source_manifest_version_id": source_release["VersionId"],
            "source_manifest_sha256": source_release["sha256"],
            "source_manifest_evidence_identity_sha256":
                source_release["evidence_identity_sha256"],
        }
        for row in catalog["objects"]:
            objects.append({
                "storage_mode": "EXISTING_EXACT_VERSION",
                "local_relpath": None,
                "bucket": row["bucket"], "key": row["key"],
                "logical_source_key": row["logical_source_key"],
                "source_kind": "catalog", "family": "catalog_at_cutoff",
                "table": row["rel"].split("/", 1)[0], "channel": None,
                "date": date, "size": row["size"],
                "sha256": row["sha256"], "seal_binding": seal_sha,
                "source_last_modified_utc": row["last_modified_utc"],
                "evidence_binding": dict(
                    catalog_evidence,
                    source_last_modified_utc=row["last_modified_utc"]),
                "research_candidate": True,
                "exposure_policy": "PENDING_ELIGIBILITY_TAG",
                "version_resolution": "PRE_RESOLVED_HISTORICAL_EXACT",
                "canonical_source": "PRE_RESOLVED_CANONICAL_VERSION",
                "expected_version_id": row["VersionId"],
            })

        late_path = os.path.join(
            warehouse_root, "corrections", "date=%s" % date,
            "late_rows.ndjson")
        ledger_path = os.path.join(
            warehouse_root, "corrections", "ledger.ndjson")
        if os.path.lexists(late_path):
            try:
                late_payload = _freeze_file(
                    late_path, max_bytes=MAX_DATE_CONTROL_BYTES)[0]
            except ReceiptError as exc:
                if exc.code != "CONTROL_TOO_LARGE":
                    raise
                raise ReceiptError(
                    "LEGACY_AUX_CONTROL_TOO_LARGE",
                    "late_rows exceeds %d bytes; use freeze-forward" %
                    MAX_DATE_CONTROL_BYTES)
        else:
            late_payload = None
        if os.path.lexists(ledger_path):
            ledger_day, ledger_rows, ledger_fingerprint = (
                _ledger_projection_file(ledger_path, date))
        else:
            ledger_day, ledger_rows, ledger_fingerprint = b"", [], None
        if ((late_payload is None) != (not ledger_rows)):
            raise ReceiptError("CORRECTIONS_INVALID",
                               "late_rows and date ledger do not agree")
        if late_payload is not None:
            _validate_correction_pair(late_payload, ledger_rows, date)
            ledger_sha = hashlib.sha256(ledger_day).hexdigest()
            corr_evidence = {"ledger_day_sha256": ledger_sha}
            objects.append(_aux_local_object(
                pending, "corrections/late_rows.ndjson", late_payload,
                bucket=bucket,
                key=_join_key(prefix, "warehouse", "corrections",
                              "date=%s" % date, "late_rows.ndjson"),
                logical_source_key=_join_key(
                    "warehouse", "corrections", "date=%s" % date,
                    "late_rows.ndjson"),
                source_kind="correction", family="corrections_at_cutoff",
                table=None, channel=None, date=date, seal_binding=seal_sha,
                evidence_binding=corr_evidence, research_candidate=True,
                exposure_policy="PENDING_ELIGIBILITY_TAG",
                version_resolution="DATE_SCOPED_CURRENT_EXACT",
                canonical_source="EXISTING_OR_PLANNED_CANONICAL_SYNC"))
            objects.append(_aux_local_object(
                pending, "corrections/ledger_day.ndjson", ledger_day,
                bucket=bucket,
                key=_join_key(
                    prefix, "warehouse", "publication-snapshots", "v1",
                    "date=%s" % date, "corrections", "ledger_day",
                    "sha256=%s" % ledger_sha, "ledger_day.ndjson"),
                logical_source_key="warehouse/corrections/ledger_day.ndjson",
                source_kind="corrections_ledger_day",
                family="corrections_at_cutoff", table=None, channel=None,
                date=date, seal_binding=seal_sha,
                evidence_binding=corr_evidence, research_candidate=True,
                exposure_policy="PENDING_ELIGIBILITY_TAG",
                version_resolution="WRITE_ONCE_CONTENT_ADDRESSED_EXACT",
                canonical_source="PLANNED_DATE_SCOPED_CONTROL_SYNC"))
            _assert_fingerprint(ledger_path, ledger_fingerprint)

        capture_path = os.path.join(
            quality_dir, "capture_gap_receipt_%s.json" % date)
        if not os.path.isfile(capture_path):
            raise ReceiptError("AUX_INPUT_INCOMPLETE",
                               "capture gap receipt is missing")
        capture_payload = _freeze_file(capture_path)[0]
        try:
            capture_receipt = json.loads(capture_payload)
        except ValueError as exc:
            raise ReceiptError("QUALITY_INVALID", str(exc))
        capture_valid, capture_reason = _validate_capture_receipt(
            capture_receipt, seal, date)
        if not capture_valid:
            raise ReceiptError("QUALITY_INVALID", capture_reason)
        objects.append(_aux_local_object(
            pending, "quality/capture_gap_receipt.json", capture_payload,
            bucket=bucket,
            key=_join_key(prefix, "control", "quality", "v1",
                          "date=%s" % date, "capture_gap_receipt.json"),
            logical_source_key=_join_key(
                "control", "quality", "v1", "date=%s" % date,
                "capture_gap_receipt.json"),
            source_kind="capture_gap_receipt", family="capture_gap_receipt",
            table=None, channel=None, date=date, seal_binding=seal_sha,
            evidence_binding="sealed firehose inventory",
            research_candidate=True,
            exposure_policy="PENDING_ELIGIBILITY_TAG",
            version_resolution="WRITE_ONCE_CURRENT",
            canonical_source="PLANNED_SEALED_DAY_CONTROL_SYNC"))

        l2_required = any(proof.get("table") == "orderbooks_full"
                          for proof in fact_proofs)
        l2_path = os.path.join(quality_dir, "l2_gaps_%s.json" % date)
        if l2_required and not os.path.isfile(l2_path):
            raise ReceiptError("AUX_INPUT_INCOMPLETE",
                               "L2 quality receipt is missing")
        if os.path.isfile(l2_path):
            l2_payload = _freeze_file(l2_path)[0]
            try:
                l2_receipt = json.loads(l2_payload)
                import research_release
                l2_quality, l2_reason = research_release.validate_l2_receipt(
                    l2_receipt, seal, date)
            except Exception as exc:
                raise ReceiptError("QUALITY_INVALID", str(exc))
            if l2_quality is None:
                raise ReceiptError("QUALITY_INVALID", l2_reason)
            objects.append(_aux_local_object(
                pending, "quality/l2_gaps.json", l2_payload,
                bucket=bucket,
                key=_join_key(prefix, "control", "quality", "v1",
                              "date=%s" % date, "l2_gaps.json"),
                logical_source_key=_join_key(
                    "control", "quality", "v1", "date=%s" % date,
                    "l2_gaps.json"),
                source_kind="l2_quality_receipt", family="l2_quality",
                table=None, channel=None, date=date, seal_binding=seal_sha,
                evidence_binding="sealed l2 inventory",
                research_candidate=True,
                exposure_policy="PENDING_ELIGIBILITY_TAG",
                version_resolution="WRITE_ONCE_CURRENT",
                canonical_source="PLANNED_SEALED_DAY_CONTROL_SYNC"))

        objects.sort(key=lambda row: row["logical_source_key"])
        projection = {
            "schema_version": AUX_SET_SCHEMA,
            "date": date,
            "bucket": bucket,
            "prefix": prefix,
            "seal_sha256": seal_sha,
            "manifest_date_sha256": manifest_digest,
            "catalog_cutoff": {
                "cutoff_utc": catalog["cutoff_utc"],
                "provenance": catalog["provenance"],
                "source_release_id": catalog["source_release_id"],
                "catalog_set_sha256": catalog["catalog_set_sha256"],
                "source_manifest": {
                    "bucket": source_release["bucket"],
                    "key": source_release["key"],
                    "VersionId": source_release["VersionId"],
                    "size": source_release["size"],
                    "sha256": source_release["sha256"],
                    "last_modified_utc":
                        source_release["last_modified_utc"],
                    "generated_at_utc":
                        source_release["generated_at_utc"],
                    "evidence_identity_sha256":
                        source_release["evidence_identity_sha256"],
                },
            },
            "objects": objects,
        }
        aux_set_sha = canonical_sha256(projection)
        descriptor = dict(projection)
        descriptor["aux_set_sha256"] = aux_set_sha
        with open(os.path.join(pending, "AUX_SET.json"), "x") as f:
            json.dump(descriptor, f, sort_keys=True, indent=2)
            f.write("\n")

        final = date_root / ("aux-set=%s" % aux_set_sha)
        if final.exists():
            existing, _rows = load_auxiliary_set(
                str(final), date, bucket, prefix, seal, seal_sha)
            if existing != descriptor:
                raise ReceiptError("AUX_SET_CONFLICT",
                                   "existing digest directory differs")
            shutil.rmtree(pending)
            pending = None
            return str(final), descriptor
        os.rename(pending, final)
        pending = None
        return str(final), descriptor
    finally:
        if pending and os.path.isdir(pending):
            shutil.rmtree(pending)


def load_auxiliary_set(path, date, bucket, prefix, seal, seal_sha):
    """Validate one explicit frozen auxiliary set; never select a latest set."""
    reader = _BundleReader(path)
    try:
        descriptor = json.loads(reader.read(
            "AUX_SET.json", max_bytes=MAX_AUX_DESCRIPTOR_BYTES)[0])
    except (UnicodeDecodeError, ValueError) as exc:
        reader.close()
        raise ReceiptError("AUX_SET_INVALID", str(exc))
    if not isinstance(descriptor, dict):
        reader.close()
        raise ReceiptError("AUX_SET_INVALID", "root is not an object")
    if descriptor.get("schema_version") != AUX_SET_SCHEMA:
        raise ReceiptError("AUX_SET_INVALID", "wrong schema_version")
    if (descriptor.get("date") != date or descriptor.get("bucket") != bucket
            or descriptor.get("prefix") != prefix):
        raise ReceiptError("AUX_SET_INVALID", "date/bucket/prefix mismatch")
    if descriptor.get("seal_sha256") != seal_sha:
        raise ReceiptError("AUX_SET_INVALID", "seal binding mismatch")
    if descriptor.get("manifest_date_sha256") != seal.get(
            "manifest_date_sha256"):
        raise ReceiptError("AUX_SET_INVALID", "manifest binding mismatch")
    cutoff = descriptor.get("catalog_cutoff")
    if not isinstance(cutoff, dict):
        raise ReceiptError("AUX_SET_INVALID", "catalog cutoff is missing")
    _parse_utc(cutoff.get("cutoff_utc"), "catalog cutoff_utc")
    if cutoff.get("provenance") not in CATALOG_PROVENANCE:
        raise ReceiptError("AUX_SET_INVALID", "catalog provenance is invalid")
    if not cutoff.get("source_release_id"):
        raise ReceiptError("AUX_SET_INVALID",
                           "verified-v2 source_release_id is missing")
    _safe_rel(cutoff["source_release_id"], "source_release_id")
    if (not isinstance(cutoff.get("catalog_set_sha256"), str)
            or not SHA256_RE.match(cutoff["catalog_set_sha256"])):
        raise ReceiptError("AUX_SET_INVALID", "catalog set digest is invalid")
    source_manifest = cutoff.get("source_manifest")
    if not isinstance(source_manifest, dict):
        raise ReceiptError("AUX_SET_INVALID", "source manifest is missing")
    source_release_id = cutoff.get("source_release_id")
    expected_source_key = _join_key(
        "research", "releases", source_release_id, "MANIFEST.json")
    if (source_manifest.get("key") != expected_source_key
            or source_manifest.get("bucket") != bucket):
        raise ReceiptError("AUX_SET_INVALID", "source manifest key invalid")
    _check_expected(source_manifest.get("size"),
                    source_manifest.get("sha256"), expected_source_key)
    source_manifest_version = source_manifest.get("VersionId")
    if (not isinstance(source_manifest_version, str)
            or not source_manifest_version.strip()
            or source_manifest_version.lower() == "null"):
        raise ReceiptError("AUX_SET_INVALID",
                           "source manifest VersionId invalid")
    source_manifest_modified = _parse_utc(
        source_manifest.get("last_modified_utc"),
        "source manifest last_modified_utc")
    if source_manifest.get("last_modified_utc") != cutoff["cutoff_utc"]:
        raise ReceiptError("AUX_SET_INVALID",
                           "cutoff is not the source manifest LastModified")
    source_manifest_generated = _parse_utc(
        source_manifest.get("generated_at_utc"),
        "source manifest generated_at_utc")
    if source_manifest_generated > source_manifest_modified:
        raise ReceiptError("AUX_SET_INVALID",
                           "source manifest generated after S3 LastModified")
    source_evidence_identity = source_manifest.get(
        "evidence_identity_sha256")
    if (not isinstance(source_evidence_identity, str)
            or not SHA256_RE.match(source_evidence_identity)):
        raise ReceiptError("AUX_SET_INVALID",
                           "source evidence identity is invalid")
    projection = {k: v for k, v in descriptor.items()
                  if k != "aux_set_sha256"}
    digest = canonical_sha256(projection)
    if descriptor.get("aux_set_sha256") != digest:
        raise ReceiptError("AUX_SET_INVALID", "aux_set_sha256 mismatch")
    if pathlib.Path(path).name != "aux-set=%s" % digest:
        raise ReceiptError("AUX_SET_INVALID", "digest directory mismatch")

    objects = descriptor.get("objects")
    if not isinstance(objects, list) or not objects:
        raise ReceiptError("AUX_SET_INVALID", "objects are missing")
    allowed_families = {
        "manifest_date_projection", "catalog_at_cutoff",
        "catalog_cutoff_evidence", "corrections_at_cutoff",
        "capture_gap_receipt", "l2_quality",
    }
    seen_keys, seen_logical, declared_files = set(), set(), set()
    enriched = []
    for raw in objects:
        if not isinstance(raw, dict):
            raise ReceiptError("AUX_SET_INVALID", "object is malformed")
        row = copy.deepcopy(raw)
        key = _safe_rel(row.get("key"), "auxiliary S3 key")
        logical = _safe_rel(row.get("logical_source_key"),
                            "auxiliary logical key")
        is_source_witness = row.get("family") == "catalog_cutoff_evidence"
        if is_source_witness:
            location_ok = (key == source_manifest["key"]
                           and row.get("bucket")
                           == source_manifest["bucket"])
        else:
            location_ok = (key.startswith(prefix + "/")
                           and row.get("bucket") == bucket)
        if not location_ok:
            raise ReceiptError("AUX_SET_INVALID", "object bucket/prefix mismatch")
        if key in seen_keys or logical in seen_logical:
            raise ReceiptError("AUX_SET_INVALID", "duplicate object key")
        seen_keys.add(key)
        seen_logical.add(logical)
        if row.get("date") != date or row.get("family") not in allowed_families:
            raise ReceiptError("AUX_SET_INVALID", "object date/family mismatch")
        _check_expected(row.get("size"), row.get("sha256"), logical)
        if row.get("seal_binding") != seal_sha:
            raise ReceiptError("AUX_SET_INVALID", "object seal mismatch")
        if is_source_witness:
            if (row.get("research_candidate") is not False
                    or row.get("exposure_policy") != "NOT_RESEARCH_EXPOSED"):
                raise ReceiptError("AUX_SET_INVALID",
                                   "source witness exposure is unsafe")
        elif (row.get("research_candidate") is not True
              or row.get("exposure_policy")
              != "PENDING_ELIGIBILITY_TAG"):
            raise ReceiptError("AUX_SET_INVALID", "auxiliary object not eligible")
        expected_version = row.get("expected_version_id")
        local_rel = row.get("local_relpath")
        storage_mode = row.get("storage_mode")
        if storage_mode == "EXISTING_EXACT_VERSION":
            if (row.get("family") != "catalog_at_cutoff"
                    or local_rel is not None or not expected_version):
                raise ReceiptError("AUX_SET_INVALID",
                                   "exact reference lacks VersionId")
        elif storage_mode == "LOCAL_FROZEN_BACKFILL":
            if expected_version is not None:
                raise ReceiptError("AUX_SET_INVALID",
                                   "local backfill preclaims a VersionId")
        elif storage_mode == \
                "EXISTING_EXACT_VERSION_WITH_LOCAL_WITNESS":
            if (row.get("family") != "catalog_cutoff_evidence"
                    or not expected_version):
                raise ReceiptError("AUX_SET_INVALID",
                                   "source witness lacks VersionId")
        else:
            raise ReceiptError("AUX_SET_INVALID", "unknown storage_mode")
        if storage_mode != "EXISTING_EXACT_VERSION":
            rel = _safe_rel(local_rel, "auxiliary local path")
            declared_files.add(rel)
            local = pathlib.Path(path, *rel.split("/"))
            if local.is_symlink():
                raise ReceiptError("AUX_SET_INVALID", "symlink in bundle")
            if row["size"] > MAX_DATE_CONTROL_BYTES:
                raise ReceiptError(
                    "AUX_SET_INVALID", "retained control exceeds byte limit")
            payload, _fingerprint = reader.read(
                rel, max_bytes=MAX_DATE_CONTROL_BYTES)
            size = len(payload)
            sha = hashlib.sha256(payload).hexdigest()
            if size != row["size"] or sha != row["sha256"]:
                raise ReceiptError("AUX_SET_INVALID", "local bytes mismatch")
            row["_local_path"] = str(local)
            row["_local_payload"] = payload
        enriched.append(row)

    actual_files = set()
    for base, dirs, files in os.walk(path):
        for name in dirs:
            if os.path.islink(os.path.join(base, name)):
                raise ReceiptError("AUX_SET_INVALID", "symlink directory")
        for name in files:
            full = os.path.join(base, name)
            if os.path.islink(full):
                raise ReceiptError("AUX_SET_INVALID", "symlink file")
            rel = os.path.relpath(full, path).replace(os.sep, "/")
            if rel != "AUX_SET.json":
                actual_files.add(rel)
    if actual_files != declared_files:
        raise ReceiptError("AUX_SET_INVALID", "declared/local file set mismatch")

    by_family = {}
    for row in enriched:
        by_family.setdefault(row["family"], []).append(row)
    if len(by_family.get("manifest_date_projection", [])) != 1:
        raise ReceiptError("AUX_SET_INVALID", "manifest projection missing")
    source_rows = by_family.get("catalog_cutoff_evidence", [])
    if len(source_rows) != 1:
        raise ReceiptError("AUX_SET_INVALID", "source manifest witness missing")
    source_row = source_rows[0]
    if (source_row.get("storage_mode")
            != "EXISTING_EXACT_VERSION_WITH_LOCAL_WITNESS"
            or source_row.get("source_kind") != "source_v2_manifest"
            or source_row.get("bucket") != source_manifest["bucket"]
            or source_row.get("key") != source_manifest["key"]
            or source_row.get("logical_source_key") != expected_source_key
            or source_row.get("expected_version_id")
            != source_manifest["VersionId"]
            or source_row.get("size") != source_manifest["size"]
            or source_row.get("sha256") != source_manifest["sha256"]
            or source_row.get("source_last_modified_utc")
            != source_manifest["last_modified_utc"]
            or source_row.get("version_resolution")
            != "EXACT_VERSION_EVIDENCE"
            or source_row.get("canonical_source")
            != "EXISTING_IMMUTABLE_V2_MANIFEST"
            or source_row.get("evidence_binding") != {
                "release_id": source_release_id,
                "cutoff_utc": cutoff["cutoff_utc"],
                "generated_at_utc": source_manifest["generated_at_utc"],
                "evidence_identity_sha256": source_evidence_identity,
            }):
        raise ReceiptError("AUX_SET_INVALID",
                           "source manifest witness semantics invalid")
    (_v2_manifest, source_generated, _source_generated_dt,
     source_catalog, calculated_evidence_identity) = \
        _validate_v2_release_manifest(
         source_row["_local_payload"], date, source_release_id,
         seal_sha)
    if (source_generated != source_manifest["generated_at_utc"]
            or calculated_evidence_identity != source_evidence_identity):
        raise ReceiptError("AUX_SET_INVALID",
                           "source manifest evidence identity mismatch")
    catalog_rows = by_family.get("catalog_at_cutoff", [])
    catalog_logical = set()
    catalog_projection = []
    for row in catalog_rows:
        marker = "warehouse/catalog/"
        if not row["logical_source_key"].startswith(marker):
            raise ReceiptError("AUX_SET_INVALID", "catalog logical key invalid")
        rel = row["logical_source_key"][len(marker):]
        catalog_logical.add(rel)
        if row["key"] != _join_key(prefix, "warehouse", "catalog", rel):
            raise ReceiptError("AUX_SET_INVALID", "catalog physical key invalid")
        if (row.get("source_kind") != "catalog"
                or row.get("version_resolution")
                != "PRE_RESOLVED_HISTORICAL_EXACT"
                or row.get("canonical_source")
                != "PRE_RESOLVED_CANONICAL_VERSION"):
            raise ReceiptError("AUX_SET_INVALID", "catalog semantics invalid")
        if _parse_utc(row.get("source_last_modified_utc"),
                      "%s source_last_modified_utc" % rel) > _parse_utc(
                          cutoff["cutoff_utc"], "catalog cutoff_utc"):
            raise ReceiptError("AUX_SET_INVALID",
                               "catalog object is newer than cutoff")
        evidence = row.get("evidence_binding")
        if (not isinstance(evidence, dict)
                or evidence.get("catalog_set_sha256")
                != cutoff["catalog_set_sha256"]
                or evidence.get("cutoff_utc") != cutoff["cutoff_utc"]
                or evidence.get("provenance") != cutoff["provenance"]
                or evidence.get("source_last_modified_utc")
                != row.get("source_last_modified_utc")
                or evidence.get("source_manifest_bucket")
                != source_manifest["bucket"]
                or evidence.get("source_manifest_key")
                != source_manifest["key"]
                or evidence.get("source_manifest_version_id")
                != source_manifest["VersionId"]
                or evidence.get("source_manifest_sha256")
                != source_manifest["sha256"]
                or evidence.get(
                    "source_manifest_evidence_identity_sha256")
                != source_evidence_identity):
            raise ReceiptError("AUX_SET_INVALID", "catalog evidence mismatch")
        catalog_projection.append({
            "bucket": row["bucket"], "key": row["key"],
            "logical_source_key": row["logical_source_key"],
            "VersionId": row["expected_version_id"],
            "size": row["size"], "sha256": row["sha256"],
        })
    if not set(CATALOG_REQUIRED) <= catalog_logical:
        raise ReceiptError("AUX_SET_INVALID", "required catalog binding missing")
    if not catalog_logical <= set(CATALOG_REQUIRED + CATALOG_OPTIONAL):
        raise ReceiptError("AUX_SET_INVALID", "unexpected catalog binding")
    if any(not row.get("expected_version_id") for row in catalog_rows):
        raise ReceiptError("AUX_SET_INVALID", "catalog VersionId missing")
    catalog_projection.sort(key=lambda row: row["logical_source_key"])
    if canonical_sha256(catalog_projection) != cutoff["catalog_set_sha256"]:
        raise ReceiptError("AUX_SET_INVALID", "catalog set digest mismatch")
    catalog_by_rel = {
        row["logical_source_key"][len("warehouse/catalog/"):]: row
        for row in catalog_rows
    }
    if set(catalog_by_rel) != set(source_catalog):
        raise ReceiptError("AUX_SET_INVALID", "catalog/v2 sets differ")
    for rel, v2_row in source_catalog.items():
        row = catalog_by_rel[rel]
        if (row["size"] != v2_row["size"]
                or row["sha256"] != v2_row["sha256"]):
            raise ReceiptError("AUX_SET_INVALID",
                               "catalog/v2 bytes differ for %s" % rel)

    manifest_row = by_family["manifest_date_projection"][0]
    expected_manifest_key = _join_key(
        prefix, "warehouse", "publication-snapshots", "v1",
        "date=%s" % date, "manifest",
        "sha256=%s" % manifest_row["sha256"], "manifest.csv")
    if (manifest_row.get("storage_mode") != "LOCAL_FROZEN_BACKFILL"
            or manifest_row.get("source_kind") != "warehouse_manifest_day"
            or manifest_row.get("logical_source_key") != "warehouse/manifest.csv"
            or manifest_row.get("key") != expected_manifest_key
            or manifest_row.get("evidence_binding")
            != {"manifest_date_sha256": seal.get("manifest_date_sha256")}
            or manifest_row.get("canonical_source")
            != "PLANNED_DATE_SCOPED_CONTROL_SYNC"):
        raise ReceiptError("AUX_SET_INVALID", "manifest semantics invalid")
    manifest_payload = manifest_row["_local_payload"]
    day_digest, day_rows = _manifest_projection_bytes(manifest_payload, date)
    all_rows = list(csv.DictReader(io.StringIO(
        manifest_payload.decode("utf-8"), newline="")))
    if (day_digest != seal.get("manifest_date_sha256")
            or len(all_rows) != len(day_rows)):
        raise ReceiptError("AUX_SET_INVALID", "manifest is not date-exact")

    corr = by_family.get("corrections_at_cutoff", [])
    if len(corr) not in (0, 2):
        raise ReceiptError("AUX_SET_INVALID", "corrections are incomplete")
    if corr:
        late = next((row for row in corr
                     if row["source_kind"] == "correction"), None)
        ledger = next((row for row in corr
                       if row["source_kind"] == "corrections_ledger_day"), None)
        if not late or not ledger:
            raise ReceiptError("AUX_SET_INVALID", "corrections roles mismatch")
        expected_late = _join_key(
            prefix, "warehouse", "corrections", "date=%s" % date,
            "late_rows.ndjson")
        expected_ledger = _join_key(
            prefix, "warehouse", "publication-snapshots", "v1",
            "date=%s" % date, "corrections", "ledger_day",
            "sha256=%s" % ledger["sha256"], "ledger_day.ndjson")
        if (late.get("key") != expected_late
                or late.get("logical_source_key") != _join_key(
                    "warehouse", "corrections", "date=%s" % date,
                    "late_rows.ndjson")
                or ledger.get("key") != expected_ledger
                or ledger.get("logical_source_key")
                != "warehouse/corrections/ledger_day.ndjson"
                or late.get("version_resolution")
                != "DATE_SCOPED_CURRENT_EXACT"
                or ledger.get("version_resolution")
                != "WRITE_ONCE_CONTENT_ADDRESSED_EXACT"):
            raise ReceiptError("AUX_SET_INVALID", "corrections keys invalid")
        late_payload = late["_local_payload"]
        ledger_payload = ledger["_local_payload"]
        ledger_projection, ledger_rows = _ledger_projection(ledger_payload, date)
        if ledger_projection != ledger_payload:
            raise ReceiptError("AUX_SET_INVALID", "ledger is not date-exact")
        _validate_correction_pair(late_payload, ledger_rows, date)

    capture = by_family.get("capture_gap_receipt", [])
    if len(capture) != 1:
        raise ReceiptError("AUX_SET_INVALID", "capture receipt missing")
    expected_capture = _join_key(
        prefix, "control", "quality", "v1", "date=%s" % date,
        "capture_gap_receipt.json")
    if (capture[0].get("key") != expected_capture
            or capture[0].get("logical_source_key") != _join_key(
                "control", "quality", "v1", "date=%s" % date,
                "capture_gap_receipt.json")
            or capture[0].get("source_kind") != "capture_gap_receipt"
            or capture[0].get("storage_mode") != "LOCAL_FROZEN_BACKFILL"):
        raise ReceiptError("AUX_SET_INVALID", "capture receipt key invalid")
    capture_payload = capture[0]["_local_payload"]
    try:
        capture_obj = json.loads(capture_payload)
    except ValueError as exc:
        raise ReceiptError("AUX_SET_INVALID", str(exc))
    valid, reason = _validate_capture_receipt(capture_obj, seal, date)
    if not valid:
        raise ReceiptError("AUX_SET_INVALID", reason)

    l2_required = any(row.get("table") == "orderbooks_full"
                      for row in seal.get("archive_file_stats", []))
    l2 = by_family.get("l2_quality", [])
    if len(l2) != int(l2_required):
        raise ReceiptError("AUX_SET_INVALID", "L2 receipt count mismatch")
    if l2:
        expected_l2 = _join_key(
            prefix, "control", "quality", "v1", "date=%s" % date,
            "l2_gaps.json")
        if (l2[0].get("key") != expected_l2
                or l2[0].get("logical_source_key") != _join_key(
                    "control", "quality", "v1", "date=%s" % date,
                    "l2_gaps.json")
                or l2[0].get("source_kind") != "l2_quality_receipt"
                or l2[0].get("storage_mode")
                != "LOCAL_FROZEN_BACKFILL"):
            raise ReceiptError("AUX_SET_INVALID", "L2 receipt key invalid")
        try:
            import research_release
            l2_obj = json.loads(l2[0]["_local_payload"])
            quality, reason = research_release.validate_l2_receipt(
                l2_obj, seal, date)
        except Exception as exc:
            raise ReceiptError("AUX_SET_INVALID", str(exc))
        if quality is None:
            raise ReceiptError("AUX_SET_INVALID", reason)
    reader.assert_stable()
    reader.close()
    return descriptor, enriched


def build_desired_inventory(date, bucket, prefix, raw_root, warehouse_root,
                            quality_dir, aux_bundle, *,
                            include_rfq_durability=False):
    """Build one frozen local inventory; this function performs no S3 call.

    Base inventory is permanently RFQ-free.  RFQ proof metadata remains
    sealed, but RFQ objects can enter only the independent overlay workflow.
    """
    if not bucket or "/" in bucket:
        raise ReceiptError("INVALID_BUCKET", repr(bucket))
    if include_rfq_durability:
        raise ReceiptError(
            "RFQ_OVERLAY_REQUIRED",
            "base canonical inventory is permanently RFQ-free")
    prefix = _safe_rel(prefix.strip("/"), "canonical prefix")
    (seal, binding, _manifest_payload, manifest_digest, _manifest_rows,
     raw_proofs, fact_proofs) = _authoritative_day_inputs(
         date, raw_root, warehouse_root)
    seal_sha = binding["sha256"]

    objects = []
    seen = {"physical": set(), "logical": set()}
    families = []

    included_raw_proofs = []
    deferred_rfq_proofs = []
    for proof in sorted(raw_proofs, key=lambda item: item["file"]):
        rel = proof["file"]
        path_date, _basename = _raw_rel_parts(rel)
        kind, channel = _raw_classification(rel)
        is_rfq = kind in {"raw_rfq", "raw_rfq_receipts"}
        if is_rfq and not include_rfq_durability:
            deferred_rfq_proofs.append(proof)
            continue
        included_raw_proofs.append(proof)
        obj = _entry(
            bucket, _join_key(prefix, "raw", rel), _join_key("raw", rel),
            kind, path_date, proof["size"], proof["sha256"], seal_sha,
            channel=channel, family="raw_durability",
            evidence_binding="seal.raw_files",
            attestation_class="DAY_SEAL_ATTESTED",
            required=not is_rfq,
            durability_scope=True, research_candidate=False,
            exposure_policy=("FORBIDDEN_RFQ_DEFAULT" if is_rfq
                             else "FORBIDDEN_RAW"),
            version_resolution="SEALED_CURRENT_EXACT")
        _add_unique(objects, seen, obj)
    families.append(_family(
        "raw_durability", "REQUIRED_CORE",
        "seal.raw_files excluding closed RFQ branch",
        len(included_raw_proofs), len(included_raw_proofs),
        "PRESENT_VERIFIED",
        semantic_sha256=canonical_sha256(included_raw_proofs)))
    if deferred_rfq_proofs:
        families.append(_family(
            "raw_rfq_deferred", "DATA_INTEGRITY_BLOCKED",
            "operator RFQ branch closed; separate future receipt required",
            0, 0, "NOT_APPLICABLE", "RFQ_BRANCH_CLOSED_NO_REPAIR",
            semantic_sha256=canonical_sha256(deferred_rfq_proofs)))

    for proof in sorted(fact_proofs, key=lambda item: item["file"]):
        rel = proof["file"]
        table = proof.get("table")
        obj = _entry(
            bucket, _join_key(prefix, "warehouse", "facts", rel),
            _join_key("warehouse", "facts", rel), "facts", date,
            proof["size"], proof["sha256"], seal_sha, table=table,
            channel=table, family="facts",
            evidence_binding="seal.archive_file_stats",
            attestation_class="DAY_SEAL_ATTESTED",
            durability_scope=True, research_candidate=True,
            exposure_policy="PENDING_ELIGIBILITY_TAG",
            version_resolution="SEALED_CURRENT_EXACT")
        _add_unique(objects, seen, obj)
    families.append(_family(
        "facts", "REQUIRED_CORE", "seal.archive_file_stats",
        len(fact_proofs), len(fact_proofs), "PRESENT_VERIFIED",
        semantic_sha256=canonical_sha256(fact_proofs)))

    seal_rel = "seals/date=%s.json" % date
    _add_bytes_object(
        objects, seen, bucket=bucket,
        key=_join_key(prefix, "warehouse", seal_rel),
        logical=_join_key("warehouse", seal_rel), kind="seal", date=date,
        payload=binding["_payload"], seal_sha=seal_sha,
        family="seal", evidence_binding="self",
        attestation_class="DAY_SEAL_ATTESTED",
        durability_scope=True, research_candidate=True,
        exposure_policy="PENDING_ELIGIBILITY_TAG",
        version_resolution="SEALED_CURRENT_EXACT")
    families.append(_family(
        "seal", "REQUIRED_CORE", "frozen full_v2 seal", 1, 1,
        "PRESENT_VERIFIED", semantic_sha256=seal_sha))
    binding.update({
        "bucket": bucket,
        "key": _join_key(prefix, "warehouse", seal_rel),
        "VersionId": None,
        "manifest_date_sha256": manifest_digest,
    })

    dim_root = os.path.join(warehouse_root, "dim", "snapshots",
                            "date=%s" % date)
    dim_present = 0
    for rel in DIM_REQUIRED:
        path = os.path.join(dim_root, rel)
        if not os.path.isfile(path):
            continue
        dim_present += 1
        _add_local_file(
            objects, seen, bucket=bucket,
            key=_join_key(prefix, "warehouse", "dim", "snapshots",
                          "date=%s" % date, rel),
            logical=_join_key("warehouse", "dim", "snapshots",
                              "date=%s" % date, rel),
            kind="dim_snapshot", date=date, seal_sha=None, path=path,
            family="dim_snapshot",
            attestation_class="PUBLICATION_CUTOFF_FROZEN",
            durability_scope=True, research_candidate=True,
            exposure_policy="PENDING_ELIGIBILITY_TAG",
            version_resolution="WRITE_ONCE_CURRENT")
    families.append(_family(
        "dim_snapshot", "REQUIRED_RESEARCH", "fixed dated dim contract",
        len(DIM_REQUIRED), dim_present,
        "PRESENT_VERIFIED" if dim_present == len(DIM_REQUIRED)
        else "INCOMPLETE",
        "NONE" if dim_present == len(DIM_REQUIRED) else "DIM_FILES_MISSING"))

    _descriptor, auxiliary = load_auxiliary_set(
        aux_bundle, date, bucket, prefix, seal, seal_sha)
    aux_by_family = {}
    for row in auxiliary:
        aux_by_family.setdefault(row["family"], []).append(row)
        obj = _entry(
            row["bucket"], row["key"], row["logical_source_key"],
            row["source_kind"], row["date"], row["size"], row["sha256"],
            seal_sha, table=row.get("table"), channel=row.get("channel"),
            local_path=row.get("_local_path"),
            local_payload=row.get("_local_payload"),
            evidence_binding=row.get("evidence_binding"),
            mutable_source=False, required=True, family=row["family"],
            attestation_class="PUBLICATION_CUTOFF_FROZEN",
            durability_scope=True,
            research_candidate=row["research_candidate"],
            exposure_policy=row["exposure_policy"],
            version_resolution=row["version_resolution"],
            canonical_source=row["canonical_source"],
            expected_version_id=row.get("expected_version_id"),
            expected_last_modified_utc=row.get(
                "source_last_modified_utc"))
        _add_unique(objects, seen, obj)

    families.append(_family(
        "manifest_date_projection", "REQUIRED_CORE",
        "date-only seal-bound manifest projection", 1,
        len(aux_by_family.get("manifest_date_projection", [])),
        "PRESENT_VERIFIED", semantic_sha256=manifest_digest))
    source_rows = aux_by_family.get("catalog_cutoff_evidence", [])
    families.append(_family(
        "catalog_cutoff_evidence", "REQUIRED_RESEARCH",
        "exact immutable v2 manifest authenticating cutoff and catalog bytes",
        1, len(source_rows),
        "PRESENT_VERIFIED" if len(source_rows) == 1 else "INCOMPLETE",
        "NONE" if len(source_rows) == 1 else "SOURCE_MANIFEST_MISSING"))
    catalog_rows = aux_by_family.get("catalog_at_cutoff", [])
    families.append(_family(
        "catalog_at_cutoff", "REQUIRED_RESEARCH",
        "v2-matched exact canonical versions current at authenticated cutoff",
        len(catalog_rows), len(catalog_rows), "PRESENT_VERIFIED",
        semantic_sha256=canonical_sha256([{
            "key": row["key"], "VersionId": row["expected_version_id"],
            "size": row["size"], "sha256": row["sha256"],
        } for row in catalog_rows])))
    corr_rows = aux_by_family.get("corrections_at_cutoff", [])
    corr_state = "PRESENT_VERIFIED" if corr_rows else "NOT_APPLICABLE"
    families.append(_family(
        "corrections_at_cutoff", "CONDITIONAL",
        "frozen late_rows plus date-only ledger", len(corr_rows),
        len(corr_rows), corr_state,
        semantic_sha256=canonical_sha256([{
            "key": row["key"], "size": row["size"],
            "sha256": row["sha256"],
        } for row in corr_rows])))
    families.append(_family(
        "capture_gap_receipt", "REQUIRED_RESEARCH",
        "sealed firehose inventory", 1,
        len(aux_by_family.get("capture_gap_receipt", [])),
        "PRESENT_VERIFIED"))
    l2_required = any(proof.get("table") == "orderbooks_full"
                      for proof in fact_proofs)
    l2_count = len(aux_by_family.get("l2_quality", []))
    families.append(_family(
        "l2_quality", "CONDITIONAL", "required when L2 facts exist",
        int(l2_required), l2_count,
        "PRESENT_VERIFIED" if l2_count else "NOT_APPLICABLE"))

    objects.sort(key=lambda obj: (obj["logical_source_key"], obj["key"]))
    binding["families"] = _finalize_families(families, objects)
    return binding, objects


def _public_object(obj):
    out = {k: v for k, v in obj.items() if not k.startswith("_")}
    return out


def _seal_projection(seal_binding):
    fields = ("date", "status", "version", "method", "bucket", "key",
              "VersionId", "size", "sha256", "manifest_date_sha256")
    return {k: seal_binding.get(k) for k in fields}


def _families_projection(seal_binding):
    fields = (
        "name", "policy", "expected_basis", "expected_count",
        "observed_count", "state", "reason_code", "semantic_sha256",
        "objects_digest",
    )
    out = [{key: family.get(key) for key in fields}
           for family in seal_binding.get("families", [])]
    out.sort(key=lambda family: family["name"])
    return out


def stable_receipt_projection(date, seal_binding, objects):
    """Return the stable, timestamp-free identity projection from the plan."""
    _validate_date(date)
    projected_seal = dict(seal_binding)
    if not projected_seal.get("VersionId"):
        seals = [o for o in objects if o.get("source_kind") == "seal"]
        if len(seals) == 1 and seals[0].get("VersionId"):
            for field in ("bucket", "key", "VersionId", "size", "sha256"):
                projected_seal[field] = seals[0].get(field)
    seen_physical, seen_logical = set(), set()
    stable = []
    fields = (
        "bucket", "key", "VersionId", "size", "sha256",
        "last_modified_utc",
        "logical_source_key", "source_kind", "family", "table", "channel",
        "date",
        "seal_binding", "evidence_binding", "durability_verified",
        "research_eligible", "eligibility_tag_state", "mutable_source",
        "required", "attestation_class", "durability_scope",
        "research_candidate", "exposure_policy", "version_resolution",
        "canonical_source",
    )
    for obj in objects:
        physical = (obj.get("bucket"), obj.get("key"))
        logical = obj.get("logical_source_key")
        if physical in seen_physical:
            raise ReceiptError("DUPLICATE_KEY",
                               "duplicate canonical key %s/%s" % physical)
        if logical in seen_logical:
            raise ReceiptError("DUPLICATE_LOGICAL_KEY",
                               "duplicate logical key %s" % logical)
        seen_physical.add(physical)
        seen_logical.add(logical)
        stable.append({k: obj.get(k) for k in fields})
    stable.sort(key=lambda o: (o["logical_source_key"], o["bucket"], o["key"]))
    return {
        "schema_version": RECEIPT_SCHEMA,
        "date": date,
        "seal": _seal_projection(projected_seal),
        "families": _families_projection(seal_binding),
        "objects": stable,
    }


def receipt_set_sha256(date, seal_binding, objects):
    return canonical_sha256(stable_receipt_projection(
        date, seal_binding, objects))


def scoped_object_set_sha256(objects, field):
    projection = [
        {"logical_source_key": obj["logical_source_key"],
         "bucket": obj["bucket"], "key": obj["key"],
         "VersionId": obj.get("VersionId"), "size": obj["size"],
         "sha256": obj["sha256"],
         "last_modified_utc": obj.get("last_modified_utc")}
        for obj in objects if obj.get(field) is True
    ]
    projection.sort(key=lambda obj: (
        obj["logical_source_key"], obj["bucket"], obj["key"]))
    return canonical_sha256(projection)


class AwsCliS3Client:
    """Read-only exact-version S3 adapter using the already-installed CLI."""

    def __init__(self, executable="aws"):
        self.executable = executable

    def _run(self, args):
        env = dict(os.environ)
        env["AWS_PAGER"] = ""
        try:
            result = subprocess.run(
                [self.executable] + args, capture_output=True, text=True,
                env=env, timeout=3600)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReceiptError("S3_CLIENT_ERROR", str(exc))
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()[-1000:]
            lower = detail.lower()
            code = ("PENDING_CANONICAL" if any(
                x in lower for x in ("not found", "nosuchkey", "404"))
                    else "S3_ACCESS_OR_REQUEST_FAILED")
            raise ReceiptError(code, detail or "CLI rc=%d" % result.returncode)
        try:
            return json.loads(result.stdout or "{}")
        except ValueError as exc:
            raise ReceiptError("S3_INVALID_RESPONSE", str(exc))

    def head(self, bucket, key, version_id=None):
        args = ["s3api", "head-object", "--bucket", bucket,
                "--key", key]
        if version_id is not None:
            args += ["--version-id", version_id]
        args += ["--checksum-mode", "ENABLED", "--output", "json"]
        return self._run(args)

    def get_exact(self, bucket, key, version_id, dest):
        return self._run(["s3api", "get-object", "--bucket", bucket,
                          "--key", key, "--version-id", version_id,
                          "--checksum-mode", "ENABLED", dest])

    def get_tags(self, bucket, key, version_id):
        """Read tags for one exact version; versionless reads are forbidden."""
        return self._run([
            "s3api", "get-object-tagging", "--bucket", bucket,
            "--key", key, "--version-id", version_id, "--output", "json",
        ])

    def list_versions(self, bucket, key):
        return self._run([
            "s3api", "list-object-versions", "--bucket", bucket,
            "--prefix", key, "--output", "json",
        ])


def _current_version_at_cutoff(history, key, cutoff_utc):
    if not isinstance(history, dict):
        raise ReceiptError("CUTOFF_HISTORY_INVALID",
                           "version history is not an object")
    if history.get("IsTruncated") is True:
        raise ReceiptError("CUTOFF_HISTORY_INCOMPLETE",
                           "version history pagination is incomplete")
    cutoff = _parse_utc(cutoff_utc, "catalog authenticated cutoff")
    candidates = []
    for field, kind in (("Versions", "VERSION"),
                        ("DeleteMarkers", "DELETE_MARKER")):
        rows = history.get(field) or []
        if not isinstance(rows, list):
            raise ReceiptError("CUTOFF_HISTORY_INVALID",
                               "%s is not a list" % field)
        for row in rows:
            if not isinstance(row, dict) or row.get("Key") != key:
                continue
            version_id = row.get("VersionId")
            if (not isinstance(version_id, str) or not version_id.strip()
                    or version_id.lower() == "null"):
                raise ReceiptError("VERSIONING_REQUIRED",
                                   "%s history has a null VersionId" % key)
            modified = _parse_utc(
                row.get("LastModified"), "%s history LastModified" % key)
            if modified <= cutoff:
                candidates.append((modified, kind, version_id))
    if not candidates:
        raise ReceiptError("CUTOFF_HISTORY_MISSING",
                           "%s has no version at authenticated cutoff" % key)
    latest_time = max(row[0] for row in candidates)
    winners = [row for row in candidates if row[0] == latest_time]
    if len(winners) != 1:
        raise ReceiptError(
            "CUTOFF_HISTORY_AMBIGUOUS",
            "%s has %d events at the cutoff-current timestamp" %
            (key, len(winners)))
    _modified, kind, version_id = winners[0]
    if kind == "DELETE_MARKER":
        raise ReceiptError("CUTOFF_DELETE_MARKER",
                           "%s was deleted at authenticated cutoff" % key)
    return version_id


def _only_original_version(history, key):
    """Prove a write-once source MANIFEST has one version and no deletion."""
    if not isinstance(history, dict):
        raise ReceiptError("SOURCE_HISTORY_INVALID",
                           "source manifest history is not an object")
    if history.get("IsTruncated") is True:
        raise ReceiptError("SOURCE_HISTORY_INCOMPLETE",
                           "source manifest history is truncated")
    versions = history.get("Versions") or []
    deletes = history.get("DeleteMarkers") or []
    if not isinstance(versions, list) or not isinstance(deletes, list):
        raise ReceiptError("SOURCE_HISTORY_INVALID",
                           "source manifest history lists are malformed")
    exact_versions = [row for row in versions
                      if isinstance(row, dict) and row.get("Key") == key]
    exact_deletes = [row for row in deletes
                     if isinstance(row, dict) and row.get("Key") == key]
    if len(exact_versions) != 1 or exact_deletes:
        raise ReceiptError(
            "SOURCE_MANIFEST_NOT_IMMUTABLE",
            "%s has %d versions and %d delete markers" %
            (key, len(exact_versions), len(exact_deletes)))
    version_id = exact_versions[0].get("VersionId")
    if (not isinstance(version_id, str) or not version_id.strip()
            or version_id.lower() == "null"):
        raise ReceiptError("VERSIONING_REQUIRED",
                           "%s source history has a null VersionId" % key)
    return version_id


def _inventory_verification_failure(obj, code, detail):
    return {"bucket": obj.get("bucket"),
            "key": obj.get("key"),
            "family": obj.get("family"),
            "canonical_source": obj.get("canonical_source"),
            "code": code, "detail": detail}


def _verify_inventory_object(index, obj, client, temp_root, metadata_only):
    """Verify one independent object and return an ordered-safe outcome."""
    label = "%s/%s" % (obj["bucket"], obj["key"])
    try:
        expected_version = obj.get("_expected_version_id")
        if obj.get("family") == "catalog_cutoff_evidence":
            resolved = _only_original_version(
                client.list_versions(obj["bucket"], obj["key"]), obj["key"])
            if resolved != expected_version:
                raise ReceiptError(
                    "SOURCE_VERSION_MISMATCH",
                    "%s original version is %s, binding selected %s" %
                    (label, resolved, expected_version))
        elif obj.get("family") == "catalog_at_cutoff":
            evidence = obj.get("evidence_binding")
            cutoff_utc = (evidence.get("cutoff_utc")
                          if isinstance(evidence, dict) else None)
            resolved = _current_version_at_cutoff(
                client.list_versions(obj["bucket"], obj["key"]),
                obj["key"], cutoff_utc)
            if resolved != expected_version:
                raise ReceiptError(
                    "CUTOFF_VERSION_MISMATCH",
                    "%s was %s at cutoff, binding selected %s" %
                    (label, resolved, expected_version))
        head = client.head(obj["bucket"], obj["key"], expected_version)
        version_id = head.get("VersionId") or head.get("version_id")
        if (not isinstance(version_id, str) or not version_id.strip()
                or version_id.lower() == "null"):
            raise ReceiptError("VERSIONING_REQUIRED",
                               "%s has no non-null VersionId" % label)
        if expected_version is not None and version_id != expected_version:
            raise ReceiptError(
                "VERSION_ID_MISMATCH",
                "%s returned VersionId %s, expected %s" %
                (label, version_id, expected_version))
        observed_modified = (head.get("LastModified")
                             or head.get("last_modified_utc"))
        if observed_modified is None:
            raise ReceiptError(
                "LAST_MODIFIED_REQUIRED", "%s returned no LastModified" % label)
        normalized_modified = _canonical_utc(
            observed_modified, "S3 LastModified")
        expected_modified = obj.get("_expected_last_modified_utc")
        if expected_modified is not None:
            if normalized_modified != _canonical_utc(
                    expected_modified, "expected LastModified"):
                raise ReceiptError(
                    "LAST_MODIFIED_MISMATCH",
                    "%s returned LastModified %s, expected %s" %
                    (label, observed_modified, expected_modified))
        content_length = head.get("ContentLength")
        if content_length is None:
            content_length = head.get("size")
        if content_length != obj["size"]:
            raise ReceiptError(
                "SIZE_MISMATCH", "%s head=%r expected=%r" %
                (label, content_length, obj["size"]))
        obj["VersionId"] = version_id
        obj["last_modified_utc"] = normalized_modified
        if metadata_only:
            obj["verification_state"] = "METADATA_ONLY"
            return obj, None
        with tempfile.TemporaryDirectory(
                prefix="object-%06d-" % index, dir=temp_root) as obj_dir:
            dest = os.path.join(obj_dir, "exact-version.bin")
            client.get_exact(obj["bucket"], obj["key"], version_id, dest)
            if not os.path.isfile(dest):
                raise ReceiptError("GET_FAILED", "%s produced no output" % label)
            got_size = os.stat(dest).st_size
            got_sha = sha256_file(dest)
            if got_size != obj["size"]:
                raise ReceiptError(
                    "SIZE_MISMATCH", "%s exact GET=%d expected=%d" %
                    (label, got_size, obj["size"]))
            if got_sha != obj["sha256"]:
                raise ReceiptError(
                    "SHA256_MISMATCH", "%s exact GET=%s expected=%s" %
                    (label, got_sha, obj["sha256"]))
        obj["durability_verified"] = True
        obj["verification_state"] = "EXACT_VERSION_FULL_SHA256"
        return obj, None
    except ReceiptError as exc:
        return None, _inventory_verification_failure(
            obj, exc.code, exc.detail)
    except Exception as exc:  # fail closed for adapters supplied by tests
        return None, _inventory_verification_failure(
            obj, "UNEXPECTED_ERROR", str(exc))


def verify_inventory(objects, client, temp_root, metadata_only=False,
                     probe_limit=None, workers=1):
    """Verify exact versions; parallelism is explicit and bounded to four."""
    if type(workers) is not int or not 1 <= workers <= MAX_VERIFY_WORKERS:
        raise ReceiptError(
            "VERIFY_WORKERS_INVALID",
            "workers must be an integer from 1 through %d" %
            MAX_VERIFY_WORKERS)
    candidates = [copy.deepcopy(o) for o in objects]
    verified = []
    failures = []
    limit = len(candidates) if probe_limit is None else max(0, probe_limit)
    selected = min(len(candidates), limit)
    os.makedirs(temp_root, exist_ok=True)
    work = list(enumerate(candidates[:selected]))
    if workers == 1 or selected < 2:
        outcomes = [
            _verify_inventory_object(
                index, obj, client, temp_root, metadata_only)
            for index, obj in work
        ]
    else:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="canonical-receipt") as executor:
            futures = [
                executor.submit(
                    _verify_inventory_object, index, obj, client,
                    temp_root, metadata_only)
                for index, obj in work
            ]
            # Futures run concurrently, but collection stays in inventory order.
            outcomes = [future.result() for future in futures]
    for obj, failure in outcomes:
        if failure is not None:
            failures.append(failure)
        else:
            verified.append(obj)
    complete = (selected == len(candidates) and not metadata_only
                and not failures and len(verified) == len(candidates))
    for obj in verified:
        # Private verifier provenance.  The receipt writer uses both fields to
        # reject metadata-only results, probes, failed runs, and slices taken
        # from an otherwise successful full inventory.  These fields never
        # enter the stable projection or emitted receipt.
        obj["_inventory_complete"] = complete
        obj["_inventory_total"] = len(candidates)
    return verified, failures, complete


def write_atomic_json(path, payload):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, sort_keys=True, indent=2,
                      ensure_ascii=True).encode("utf-8") + b"\n"
    tmp = path.with_name(".%s.tmp.%d" % (path.name, os.getpid()))
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass
    return path


def _final_seal_binding(seal_binding, objects):
    out = {k: v for k, v in seal_binding.items()
           if k != "local_path" and not k.startswith("_")}
    seals = [o for o in objects if o.get("source_kind") == "seal"]
    if len(seals) != 1:
        raise ReceiptError("SEAL_BINDING_MISMATCH",
                           "expected one seal object, got %d" % len(seals))
    obj = seals[0]
    for field in ("bucket", "key", "VersionId", "size", "sha256"):
        value = obj.get(field)
        if value is None:
            raise ReceiptError("SEAL_BINDING_MISMATCH",
                               "seal has no %s" % field)
        out[field] = value
    for item in objects:
        attestation = item.get("attestation_class")
        if attestation in ("DAY_SEAL_ATTESTED", "SEAL_PROJECTION_BOUND"):
            if item.get("seal_binding") != obj.get("sha256"):
                raise ReceiptError(
                    "SEAL_BINDING_MISMATCH",
                    "%s is not bound to the frozen seal" % item.get("key"))
        elif attestation == "PUBLICATION_CUTOFF_FROZEN":
            if (item.get("seal_binding") is not None
                    and item.get("seal_binding") != obj.get("sha256")):
                raise ReceiptError(
                    "SEAL_BINDING_MISMATCH",
                    "%s carries a different seal binding" % item.get("key"))
        elif item.get("seal_binding") is not None:
            raise ReceiptError(
                "SEAL_BINDING_MISMATCH",
                "%s falsely claims direct seal binding" % item.get("key"))
    return out


def write_shadow_receipt(output_root, date, seal_binding, objects,
                         verified_at, commit):
    """Write/reuse an immutable local-only receipt after complete verification."""
    _parse_utc(verified_at, "receipt verified_at_utc")
    if not isinstance(commit, str) or not commit.strip():
        raise ReceiptError(
            "INCOMPLETE_VERIFICATION", "publisher code commit is missing")
    if not objects:
        raise ReceiptError("INCOMPLETE_VERIFICATION", "object set is empty")
    if seal_binding.get("date") != date:
        raise ReceiptError("SEAL_BINDING_MISMATCH",
                           "receipt date differs from frozen seal")
    blockers = _family_blockers(seal_binding.get("families", []))
    if blockers:
        raise ReceiptError(
            "INCOMPLETE_VERIFICATION",
            "required family is incomplete: %s" %
            ",".join(sorted(item["name"] for item in blockers)))
    totals = {obj.get("_inventory_total") for obj in objects}
    if (any(obj.get("_inventory_complete") is not True for obj in objects)
            or totals != {len(objects)}):
        raise ReceiptError(
            "INCOMPLETE_VERIFICATION",
            "partial object set is not complete or authoritative")
    for obj in objects:
        if (obj.get("durability_verified") is not True
                or not obj.get("VersionId")
                or obj.get("verification_state")
                != "EXACT_VERSION_FULL_SHA256"):
            raise ReceiptError(
                "INCOMPLETE_VERIFICATION",
                "%s is not exact-version byte verified" % obj.get("key"))
    final_seal = _final_seal_binding(seal_binding, objects)
    digest = receipt_set_sha256(date, final_seal, objects)
    public_objects = []
    for obj in objects:
        public = _public_object(obj)
        public["verified_at_utc"] = verified_at
        public["publisher_code_commit"] = commit
        public_objects.append(public)
    payload = {
        "schema_version": RECEIPT_SCHEMA,
        "state": "RECEIPT_VERIFIED_SHADOW",
        "authority": "SHADOW_LOCAL_ONLY",
        "authoritative": False,
        "s3_published": False,
        "prune_eligible": False,
        "date": date,
        "receipt_set_sha256": digest,
        "seal": _seal_projection(final_seal),
        "families": _families_projection(final_seal),
        "durability_set_sha256": scoped_object_set_sha256(
            objects, "durability_scope"),
        "research_candidate_set_sha256": scoped_object_set_sha256(
            objects, "research_candidate"),
        "objects": public_objects,
        "verified_at_utc": verified_at,
        "publisher_code_commit": commit,
    }
    path = pathlib.Path(output_root) / ("date=%s" % date) / \
        ("receipt-%s.json" % digest)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReceiptError(
                "RECEIPT_CONFLICT",
                "immutable receipt conflict while reading %s: %s" %
                (path, exc))
        fixed = {
            "schema_version": RECEIPT_SCHEMA,
            "state": "RECEIPT_VERIFIED_SHADOW",
            "authority": "SHADOW_LOCAL_ONLY",
            "authoritative": False,
            "s3_published": False,
            "prune_eligible": False,
            "date": date,
            "receipt_set_sha256": digest,
        }
        if any(existing.get(key) != value for key, value in fixed.items()):
            raise ReceiptError(
                "RECEIPT_CONFLICT",
                "immutable receipt mismatch at %s" % path)
        existing_binding = dict(existing.get("seal", {}))
        existing_binding["families"] = existing.get("families", [])
        existing_projection = stable_receipt_projection(
            date, existing_binding, existing.get("objects", []))
        if canonical_sha256(existing_projection) != digest:
            raise ReceiptError("RECEIPT_CONFLICT",
                               "existing immutable receipt projection mismatch")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, sort_keys=True, indent=2,
                      ensure_ascii=True).encode("utf-8") + b"\n"
    tmp = path.with_name(".%s.tmp.%d" % (path.name, os.getpid()))
    with open(tmp, "xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.link(tmp, path)
    except FileExistsError:
        tmp.unlink(missing_ok=True)
        return write_shadow_receipt(output_root, date, seal_binding, objects,
                                    verified_at, commit)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def _code_commit():
    try:
        return gp.require_clean_head(
            wc.ROOT, CANONICAL_RECEIPT_PROVENANCE_PATHS)
    except gp.GitProvenanceError as exc:
        raise ReceiptError(exc.code, exc.detail)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _default_output_root():
    return os.path.join(wc.ROOT, "work", "live", "canonical_receipts",
                        "shadow")


def _default_aux_root():
    return os.path.join(wc.ROOT, "work", "live", "canonical_receipts",
                        "auxiliary")


def _add_sources(ap):
    ap.add_argument("--date", required=True)
    ap.add_argument("--bucket", default="kalshi-vault-ritcardo")
    ap.add_argument("--prefix", default="ec2")
    ap.add_argument("--raw-root")
    ap.add_argument("--warehouse-root")
    ap.add_argument("--quality-dir",
                    default=os.path.join(wc.ROOT, "work", "event_packs"))


def _add_common(ap):
    _add_sources(ap)
    ap.add_argument("--aux-bundle", required=True)
    ap.add_argument("--output-root", default=_default_output_root())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser(
        "freeze-aux", help="freeze small date controls plus catalog bindings")
    _add_sources(freeze)
    freeze.add_argument("--catalog-bindings", required=True)
    freeze.add_argument("--aux-root", default=_default_aux_root())
    plan = sub.add_parser("plan", help="build the desired inventory only")
    _add_common(plan)
    shadow = sub.add_parser(
        "shadow", help="read and verify exact S3 versions; never write S3")
    _add_common(shadow)
    shadow.add_argument("--aws-cli", default="aws")
    shadow.add_argument("--metadata-only", action="store_true")
    shadow.add_argument("--probe-limit", type=int)
    args = ap.parse_args(argv)
    checked_at = _now()
    output_root = (os.path.abspath(args.output_root)
                   if args.command in ("plan", "shadow") else None)
    status_path = (pathlib.Path(output_root) / ("date=%s" % args.date)
                   / "SHADOW_STATUS.json"
                   if args.command == "shadow" else None)

    if args.command == "shadow":
        try:
            _validate_date(args.date)
            write_atomic_json(status_path, {
                "schema_version": SHADOW_STATUS_SCHEMA,
                "state": "SHADOW_RUNNING",
                "date": args.date,
                "checked_at_utc": checked_at,
                "inventory_objects": 0,
                "inventory_bytes": 0,
                "verified_objects": 0,
                "complete": False,
                "content_verification_complete": False,
                "metadata_only": bool(args.metadata_only),
                "probe_limit": args.probe_limit,
                "failures": [],
                "authority": "SHADOW_LOCAL_ONLY",
                "s3_writes": 0,
                "tag_writes": 0,
                "prune_changes": 0,
            })
        except (ReceiptError, OSError) as exc:
            print("BLOCKED_INTEGRITY unable to invalidate shadow status: %s" %
                  exc, file=sys.stderr)
            return 2

    cfg = wc.load_config()
    raw_root = os.path.abspath(args.raw_root or cfg["raw_root"])
    warehouse_root = os.path.abspath(
        args.warehouse_root or cfg["warehouse_root"])
    quality_dir = os.path.abspath(args.quality_dir)

    if args.command == "freeze-aux":
        try:
            aux_path, descriptor = freeze_auxiliary_set(
                args.date, args.bucket, args.prefix, raw_root, warehouse_root,
                quality_dir, os.path.abspath(args.aux_root),
                os.path.abspath(args.catalog_bindings))
        except ReceiptError as exc:
            print("BLOCKED_INTEGRITY %s" % exc, file=sys.stderr)
            return 2
        print(json.dumps({
            "state": "AUXILIARY_SET_FROZEN",
            "date": args.date,
            "path": aux_path,
            "aux_set_sha256": descriptor["aux_set_sha256"],
            "objects": len(descriptor["objects"]),
            "local_bytes": sum(
                row["size"] for row in descriptor["objects"]
                if row["storage_mode"] == "LOCAL_FROZEN_BACKFILL"),
            "catalog_bytes_copied": 0,
            "s3_writes": 0,
        }, sort_keys=True))
        return 0

    try:
        seal_binding, objects = build_desired_inventory(
            args.date, args.bucket, args.prefix, raw_root, warehouse_root,
            quality_dir, os.path.abspath(args.aux_bundle))
    except ReceiptError as exc:
        print("BLOCKED_INTEGRITY %s" % exc, file=sys.stderr)
        if args.command == "shadow":
            write_atomic_json(status_path, {
                "schema_version": SHADOW_STATUS_SCHEMA,
                "state": "BLOCKED_INTEGRITY",
                "date": args.date,
                "checked_at_utc": checked_at,
                "inventory_objects": 0,
                "inventory_bytes": 0,
                "verified_objects": 0,
                "complete": False,
                "content_verification_complete": False,
                "metadata_only": bool(args.metadata_only),
                "probe_limit": args.probe_limit,
                "failures": [{
                    "bucket": None, "key": None,
                    "family": "local_precheck",
                    "canonical_source": None,
                    "code": exc.code, "detail": exc.detail,
                }],
                "authority": "SHADOW_LOCAL_ONLY",
                "s3_writes": 0,
                "tag_writes": 0,
                "prune_changes": 0,
            })
        return 2

    if args.command == "plan":
        kinds = {}
        for obj in objects:
            kinds[obj["source_kind"]] = kinds.get(obj["source_kind"], 0) + 1
        print(json.dumps({
            "state": "INVENTORY_PLANNED",
            "date": args.date,
            "objects": len(objects),
            "bytes": sum(o["size"] for o in objects),
            "kinds": kinds,
            "durability_objects": sum(
                obj["durability_scope"] for obj in objects),
            "research_candidate_objects": sum(
                obj["research_candidate"] for obj in objects),
            "families": [{
                "name": family["name"],
                "policy": family["policy"],
                "state": family["state"],
                "reason_code": family["reason_code"],
                "observed_count": family["observed_count"],
                "expected_count": family["expected_count"],
            } for family in seal_binding.get("families", [])],
            "s3_writes": 0,
            "tag_writes": 0,
            "prune_changes": 0,
        }, sort_keys=True))
        return 0

    blockers = _family_blockers(seal_binding.get("families", []))
    if blockers:
        verified, complete = [], False
        failures = [{
            "bucket": None,
            "key": None,
            "family": item["name"],
            "canonical_source": None,
            "code": "LOCAL_FAMILY_%s" % item["state"],
            "detail": item["reason_code"],
        } for item in blockers]
        blocker_names = {item["name"] for item in blockers}
        if blocker_names <= {"capture_gap_receipt", "l2_quality"}:
            state = "PENDING_QUALITY_GENERATION"
        elif "corrections_at_cutoff" in blocker_names:
            state = "BLOCKED_INTEGRITY"
        else:
            state = "PENDING_LOCAL_INPUT"
    else:
        client = AwsCliS3Client(args.aws_cli)
        temp_root = os.path.join(output_root, ".tmp")
        verified, failures, complete = verify_inventory(
            objects, client, temp_root, metadata_only=args.metadata_only,
            probe_limit=args.probe_limit)
        pending_codes = {"PENDING_CANONICAL", "S3_ACCESS_OR_REQUEST_FAILED",
                         "S3_CLIENT_ERROR"}
        if failures and all(f["code"] in pending_codes for f in failures):
            if any(f.get("canonical_source") ==
                   "PLANNED_SEALED_DAY_CONTROL_SYNC" for f in failures):
                state = "PENDING_QUALITY_CANONICAL"
            else:
                state = "PENDING_CANONICAL"
        elif failures:
            state = "BLOCKED_INTEGRITY"
        elif not complete:
            state = "PENDING_CONTENT_VERIFICATION"
        else:
            state = "CONTENT_VERIFIED"

    receipt_path = None
    if state == "CONTENT_VERIFIED":
        try:
            receipt_path = write_shadow_receipt(
                output_root, args.date, seal_binding, verified, checked_at,
                _code_commit())
            state = "RECEIPT_VERIFIED_SHADOW"
        except ReceiptError as exc:
            failures.append({
                "bucket": None, "key": None, "family": "shadow_receipt",
                "canonical_source": None, "code": exc.code,
                "detail": exc.detail,
            })
            state = "BLOCKED_INTEGRITY"
        except OSError as exc:
            failures.append({
                "bucket": None, "key": None, "family": "shadow_receipt",
                "canonical_source": None,
                "code": "LOCAL_RECEIPT_WRITE_FAILED", "detail": str(exc),
            })
            state = "BLOCKED_INTEGRITY"

    status = {
        "schema_version": SHADOW_STATUS_SCHEMA,
        "state": state,
        "date": args.date,
        "checked_at_utc": checked_at,
        "inventory_objects": len(objects),
        "inventory_bytes": sum(o["size"] for o in objects),
        "verified_objects": sum(
            o.get("durability_verified") is True for o in verified),
        "complete": state == "RECEIPT_VERIFIED_SHADOW",
        "content_verification_complete": complete,
        "metadata_only": bool(args.metadata_only),
        "probe_limit": args.probe_limit,
        "failures": failures,
        "authority": "SHADOW_LOCAL_ONLY",
        "s3_writes": 0,
        "tag_writes": 0,
        "prune_changes": 0,
    }
    write_atomic_json(status_path, status)
    print(json.dumps({"state": state, "status": str(status_path),
                      "receipt": str(receipt_path) if receipt_path else None,
                      "failures": len(failures)}, sort_keys=True))
    return (0 if state == "RECEIPT_VERIFIED_SHADOW"
            else 3 if state.startswith("PENDING") else 2)


if __name__ == "__main__":
    raise SystemExit(main())
