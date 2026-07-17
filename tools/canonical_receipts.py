#!/usr/bin/env python3
"""Build exact-version canonical S3 receipt shadows for one sealed day.

W-PUB-REF-01A phase 1 is deliberately read-only against S3.  It inventories
the objects a sealed day depends on, binds the current canonical object to a
non-null VersionId with HEAD, downloads that exact version, and verifies the
full SHA-256.  A successful run may write an immutable *local shadow* receipt;
it never writes S3, changes tags, or participates in raw pruning.

Commands:

  canonical_receipts.py plan   --date YYYY-MM-DD
  canonical_receipts.py shadow --date YYYY-MM-DD [--metadata-only]

The shadow output is intentionally branded non-authoritative.  A later,
separately audited phase must conditional-create the durable S3 receipt and
read it back before any prune-consumable local index may exist.
"""
import argparse
import copy
import csv
import datetime
import hashlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warehouse_common as wc  # noqa: E402


RECEIPT_SCHEMA = "canonical-object-receipt-v1"
SHADOW_STATUS_SCHEMA = "canonical-receipt-shadow-status-v1"
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
FAMILY_BLOCKING_STATES = {
    "INCOMPLETE", "INVALID", "CHANGED_DURING_CUTOFF",
}


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


def _file_attestation(path):
    """Hash a regular file and reject a source that changed during hashing."""
    if not os.path.isfile(path) or os.path.islink(path):
        raise ReceiptError("LOCAL_MISSING", "regular file required: %s" % path)
    before = os.stat(path)
    digest = sha256_file(path)
    after = os.stat(path)
    stable = (before.st_dev, before.st_ino, before.st_size,
              before.st_mtime_ns, before.st_ctime_ns) == (
                  after.st_dev, after.st_ino, after.st_size,
                  after.st_mtime_ns, after.st_ctime_ns)
    if not stable:
        raise ReceiptError("LOCAL_SOURCE_CHANGED",
                           "file changed while hashing: %s" % path)
    return after.st_size, digest


def _freeze_file(path):
    """Read one small control file once and prove the bytes were stable."""
    if not os.path.isfile(path) or os.path.islink(path):
        raise ReceiptError("LOCAL_MISSING", "regular file required: %s" % path)
    before = os.stat(path)
    with open(path, "rb") as f:
        payload = f.read()
    after = os.stat(path)
    before_key = (before.st_dev, before.st_ino, before.st_size,
                  before.st_mtime_ns, before.st_ctime_ns)
    after_key = (after.st_dev, after.st_ino, after.st_size,
                 after.st_mtime_ns, after.st_ctime_ns)
    if before_key != after_key or len(payload) != after.st_size:
        raise ReceiptError("LOCAL_SOURCE_CHANGED",
                           "file changed while freezing: %s" % path)
    return payload, after_key


def _assert_fingerprint(path, expected):
    try:
        st = os.stat(path)
    except OSError as exc:
        raise ReceiptError("LOCAL_SOURCE_CHANGED", "%s: %s" % (path, exc))
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
           evidence_binding=None, mutable_source=False, required=True,
           family=None, attestation_class="PUBLICATION_CUTOFF_FROZEN",
           durability_scope=True, research_candidate=False,
           exposure_policy="NOT_RESEARCH_EXPOSED",
           version_resolution="CURRENT_AT_CUTOFF",
           canonical_source="EXISTING_CANONICAL_SYNC"):
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
    return out


def _add_local_file(objects, seen, *, bucket, key, logical, kind, date,
                    seal_sha, path, table=None, channel=None,
                    evidence_binding=None, mutable=False, required=True,
                    family=None, attestation_class="PUBLICATION_CUTOFF_FROZEN",
                    durability_scope=True, research_candidate=False,
                    exposure_policy="NOT_RESEARCH_EXPOSED",
                    version_resolution="CURRENT_AT_CUTOFF",
                    canonical_source="EXISTING_CANONICAL_SYNC"):
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
                 canonical_source=canonical_source)
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


def _ledger_projection(payload, date):
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReceiptError("CORRECTIONS_INVALID", str(exc))
    lines, rows = [], []
    for lineno, raw in enumerate(text.splitlines(), 1):
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
        if row.get("exchange_date") == date or row.get("date") == date:
            lines.append(raw.strip())
            rows.append(row)
    projection = (("\n".join(lines) + "\n").encode("utf-8")
                  if lines else b"")
    return projection, rows


def _count_ndjson(payload, label):
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReceiptError("CORRECTIONS_INVALID", "%s: %s" % (label, exc))
    count = 0
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except ValueError as exc:
            raise ReceiptError("CORRECTIONS_INVALID",
                               "%s line %d: %s" % (label, lineno, exc))
        if not isinstance(row, dict):
            raise ReceiptError("CORRECTIONS_INVALID",
                               "%s line %d is not an object" % (label, lineno))
        count += 1
    return count


def build_desired_inventory(date, bucket, prefix, raw_root, warehouse_root,
                            quality_dir):
    """Build one frozen local inventory; this function performs no S3 call."""
    if not bucket or "/" in bucket:
        raise ReceiptError("INVALID_BUCKET", repr(bucket))
    prefix = _safe_rel(prefix.strip("/"), "canonical prefix")
    seal, binding = load_verified_seal(warehouse_root, date)
    seal_sha = binding["sha256"]
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

    objects = []
    seen = {"physical": set(), "logical": set()}
    families = []

    for proof in sorted(raw_proofs, key=lambda item: item["file"]):
        rel = proof["file"]
        path_date, basename = _raw_rel_parts(rel)
        kind, channel = _raw_classification(rel)
        obj = _entry(
            bucket, _join_key(prefix, "raw", rel), _join_key("raw", rel),
            kind, path_date, proof["size"], proof["sha256"], seal_sha,
            channel=channel, family="raw_durability",
            evidence_binding="seal.raw_files",
            attestation_class="DAY_SEAL_ATTESTED",
            durability_scope=True, research_candidate=False,
            exposure_policy=("FORBIDDEN_RFQ_DEFAULT"
                             if RFQ_RE.match(basename) else "FORBIDDEN_RAW"),
            version_resolution="SEALED_CURRENT_EXACT")
        _add_unique(objects, seen, obj)
    families.append(_family(
        "raw_durability", "REQUIRED_CORE", "seal.raw_files",
        len(raw_proofs), len(raw_proofs), "PRESENT_VERIFIED",
        semantic_sha256=canonical_sha256(raw_proofs)))

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

    _add_bytes_object(
        objects, seen, bucket=bucket,
        key=_join_key(prefix, "warehouse", "manifest.csv"),
        logical="warehouse/manifest.csv", kind="warehouse_manifest",
        date=date, payload=manifest_payload, seal_sha=seal_sha,
        family="manifest_date_projection",
        evidence_binding={"manifest_date_sha256": manifest_digest},
        mutable_source=True,
        attestation_class="SEAL_PROJECTION_BOUND",
        durability_scope=True, research_candidate=False,
        exposure_policy="CONTROL_PROVENANCE_ONLY",
        version_resolution="CURRENT_AT_CUTOFF")
    families.append(_family(
        "manifest_date_projection", "REQUIRED_CORE",
        "warehouse_common.manifest_date_sha256", 1, 1,
        "PRESENT_VERIFIED", semantic_sha256=manifest_digest))

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

    catalog_root = os.path.join(warehouse_root, "catalog")
    catalog_present = 0
    for rel in CATALOG_REQUIRED + CATALOG_OPTIONAL:
        path = os.path.join(catalog_root, *rel.split("/"))
        if not os.path.isfile(path):
            continue
        if rel in CATALOG_REQUIRED:
            catalog_present += 1
        _add_local_file(
            objects, seen, bucket=bucket,
            key=_join_key(prefix, "warehouse", "catalog", rel),
            logical=_join_key("warehouse", "catalog", rel),
            kind="catalog", date=date, seal_sha=None, path=path,
            family="catalog_at_cutoff", mutable=True,
            attestation_class="PUBLICATION_CUTOFF_FROZEN",
            durability_scope=True, research_candidate=True,
            exposure_policy="PENDING_ELIGIBILITY_TAG",
            version_resolution="CURRENT_AT_CUTOFF")
    families.append(_family(
        "catalog_at_cutoff", "REQUIRED_RESEARCH",
        "fixed catalog allowlist at cutoff", len(CATALOG_REQUIRED),
        catalog_present,
        "PRESENT_VERIFIED" if catalog_present == len(CATALOG_REQUIRED)
        else "INCOMPLETE",
        "NONE" if catalog_present == len(CATALOG_REQUIRED)
        else "CATALOG_FILES_MISSING"))

    late_path = os.path.join(
        warehouse_root, "corrections", "date=%s" % date,
        "late_rows.ndjson")
    ledger_path = os.path.join(
        warehouse_root, "corrections", "ledger.ndjson")
    late_payload = _freeze_file(late_path)[0]         if os.path.isfile(late_path) else None
    ledger_payload = _freeze_file(ledger_path)[0]         if os.path.isfile(ledger_path) else None
    ledger_projection, ledger_rows = (
        _ledger_projection(ledger_payload, date)
        if ledger_payload is not None else (b"", []))
    late_count = (_count_ndjson(late_payload, "late_rows")
                  if late_payload is not None else 0)
    ledger_count = 0
    ledger_valid = True
    for row in ledger_rows:
        n_rows = row.get("n_rows")
        if (row.get("event") != "LATE_FACT_DIVERTED_TO_CORRECTIONS"
                or not isinstance(n_rows, int) or isinstance(n_rows, bool)
                or n_rows < 0):
            ledger_valid = False
            break
        ledger_count += n_rows
    corrections_valid = ledger_valid and late_count == ledger_count
    ledger_day_sha = hashlib.sha256(ledger_projection).hexdigest()

    if late_payload is not None:
        _add_bytes_object(
            objects, seen, bucket=bucket,
            key=_join_key(prefix, "warehouse", "corrections",
                          "date=%s" % date, "late_rows.ndjson"),
            logical=_join_key("warehouse", "corrections",
                              "date=%s" % date, "late_rows.ndjson"),
            kind="correction", date=date, payload=late_payload,
            seal_sha=None, family="corrections_at_cutoff",
            evidence_binding={"ledger_day_sha256": ledger_day_sha},
            mutable_source=True,
            attestation_class="PUBLICATION_CUTOFF_FROZEN",
            durability_scope=True, research_candidate=True,
            exposure_policy="PENDING_ELIGIBILITY_TAG",
            version_resolution="CURRENT_AT_CUTOFF")
    if ledger_rows:
        _add_bytes_object(
            objects, seen, bucket=bucket,
            key=_join_key(prefix, "warehouse", "corrections",
                          "ledger.ndjson"),
            logical="warehouse/corrections/ledger.ndjson",
            kind="corrections_ledger", date=date, payload=ledger_payload,
            seal_sha=None, family="corrections_at_cutoff",
            evidence_binding={"ledger_day_sha256": ledger_day_sha},
            mutable_source=True,
            attestation_class="PUBLICATION_CUTOFF_FROZEN",
            durability_scope=True, research_candidate=False,
            exposure_policy="CONTROL_PROVENANCE_ONLY",
            version_resolution="CURRENT_AT_CUTOFF")
    corr_count = int(late_payload is not None) + int(bool(ledger_rows))
    corr_state = ("INVALID" if not corrections_valid else
                  "PRESENT_VERIFIED" if corr_count else "NOT_APPLICABLE")
    families.append(_family(
        "corrections_at_cutoff", "CONDITIONAL",
        "date partition plus date-filtered ledger", corr_count, corr_count,
        corr_state,
        "CORRECTIONS_LEDGER_MISMATCH" if not corrections_valid else "NONE",
        semantic_sha256=ledger_day_sha))

    capture_path = os.path.join(
        quality_dir, "capture_gap_receipt_%s.json" % date)
    capture_valid = False
    capture_reason = "LOCAL_CAPTURE_RECEIPT_ABSENT"
    if os.path.isfile(capture_path):
        capture_payload, _ = _freeze_file(capture_path)
        try:
            capture_receipt = json.loads(capture_payload)
        except ValueError as exc:
            raise ReceiptError("QUALITY_INVALID", str(exc))
        capture_valid, capture_reason = _validate_capture_receipt(
            capture_receipt, seal, date)
        if capture_valid:
            _add_bytes_object(
                objects, seen, bucket=bucket,
                key=_join_key(prefix, "control", "quality", "v1",
                              "date=%s" % date,
                              "capture_gap_receipt.json"),
                logical=_join_key("control", "quality", "v1",
                                  "date=%s" % date,
                                  "capture_gap_receipt.json"),
                kind="capture_gap_receipt", date=date,
                payload=capture_payload, seal_sha=None,
                family="capture_gap_receipt",
                evidence_binding="sealed firehose inventory",
                attestation_class="PUBLICATION_CUTOFF_FROZEN",
                durability_scope=True, research_candidate=True,
                exposure_policy="PENDING_ELIGIBILITY_TAG",
                version_resolution="WRITE_ONCE_CURRENT",
                canonical_source="PLANNED_SEALED_DAY_CONTROL_SYNC")
    families.append(_family(
        "capture_gap_receipt", "REQUIRED_RESEARCH",
        "sealed firehose inventory", 1, int(capture_valid),
        "PRESENT_VERIFIED" if capture_valid else "INCOMPLETE",
        "NONE" if capture_valid else capture_reason))

    l2_required = any(proof.get("table") == "orderbooks_full"
                      for proof in fact_proofs)
    l2_path = os.path.join(quality_dir, "l2_gaps_%s.json" % date)
    l2_valid = False
    l2_reason = "LOCAL_L2_RECEIPT_ABSENT"
    if os.path.isfile(l2_path):
        l2_payload, _ = _freeze_file(l2_path)
        try:
            l2_receipt = json.loads(l2_payload)
            import research_release
            l2_quality, l2_reason = research_release.validate_l2_receipt(
                l2_receipt, seal, date)
        except Exception as exc:
            raise ReceiptError("QUALITY_INVALID", str(exc))
        l2_valid = l2_quality is not None
        if l2_valid:
            _add_bytes_object(
                objects, seen, bucket=bucket,
                key=_join_key(prefix, "control", "quality", "v1",
                              "date=%s" % date, "l2_gaps.json"),
                logical=_join_key("control", "quality", "v1",
                                  "date=%s" % date, "l2_gaps.json"),
                kind="l2_quality_receipt", date=date, payload=l2_payload,
                seal_sha=None, family="l2_quality",
                evidence_binding="sealed l2 inventory",
                attestation_class="PUBLICATION_CUTOFF_FROZEN",
                durability_scope=True, research_candidate=True,
                exposure_policy="PENDING_ELIGIBILITY_TAG",
                version_resolution="WRITE_ONCE_CURRENT",
                canonical_source="PLANNED_SEALED_DAY_CONTROL_SYNC")
    l2_state = ("PRESENT_VERIFIED" if l2_valid else
                "INCOMPLETE" if l2_required else "NOT_APPLICABLE")
    families.append(_family(
        "l2_quality", "CONDITIONAL", "required when L2 facts exist",
        int(l2_required), int(l2_valid), l2_state,
        "NONE" if l2_valid or not l2_required else l2_reason))

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
         "sha256": obj["sha256"]}
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

    def head(self, bucket, key):
        return self._run(["s3api", "head-object", "--bucket", bucket,
                          "--key", key, "--checksum-mode", "ENABLED",
                          "--output", "json"])

    def get_exact(self, bucket, key, version_id, dest):
        return self._run(["s3api", "get-object", "--bucket", bucket,
                          "--key", key, "--version-id", version_id,
                          "--checksum-mode", "ENABLED", dest])


def verify_inventory(objects, client, temp_root, metadata_only=False,
                     probe_limit=None):
    """Verify HEAD VersionId then GET that exact version and full SHA-256."""
    candidates = [copy.deepcopy(o) for o in objects]
    verified = []
    failures = []
    limit = len(candidates) if probe_limit is None else max(0, probe_limit)
    selected = min(len(candidates), limit)
    os.makedirs(temp_root, exist_ok=True)
    for index, obj in enumerate(candidates):
        if index >= selected:
            continue
        label = "%s/%s" % (obj["bucket"], obj["key"])
        try:
            head = client.head(obj["bucket"], obj["key"])
            version_id = head.get("VersionId") or head.get("version_id")
            if (not isinstance(version_id, str) or not version_id.strip()
                    or version_id.lower() == "null"):
                raise ReceiptError("VERSIONING_REQUIRED",
                                   "%s has no non-null VersionId" % label)
            content_length = head.get("ContentLength")
            if content_length is None:
                content_length = head.get("size")
            if content_length != obj["size"]:
                raise ReceiptError(
                    "SIZE_MISMATCH", "%s head=%r expected=%r" %
                    (label, content_length, obj["size"]))
            obj["VersionId"] = version_id
            if metadata_only:
                obj["verification_state"] = "METADATA_ONLY"
                verified.append(obj)
                continue
            obj_dir = tempfile.mkdtemp(prefix="object-", dir=temp_root)
            dest = os.path.join(obj_dir, "exact-version.bin")
            try:
                client.get_exact(obj["bucket"], obj["key"], version_id, dest)
                if not os.path.isfile(dest):
                    raise ReceiptError("GET_FAILED",
                                       "%s produced no output" % label)
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
            finally:
                try:
                    if os.path.isfile(dest):
                        os.remove(dest)
                    os.rmdir(obj_dir)
                except OSError:
                    pass
            obj["durability_verified"] = True
            obj["verification_state"] = "EXACT_VERSION_FULL_SHA256"
            verified.append(obj)
        except ReceiptError as exc:
            failures.append({"bucket": obj.get("bucket"),
                             "key": obj.get("key"),
                             "family": obj.get("family"),
                             "canonical_source": obj.get("canonical_source"),
                             "code": exc.code, "detail": exc.detail})
        except Exception as exc:  # fail closed for adapters supplied by tests
            failures.append({"bucket": obj.get("bucket"),
                             "key": obj.get("key"),
                             "family": obj.get("family"),
                             "canonical_source": obj.get("canonical_source"),
                             "code": "UNEXPECTED_ERROR",
                             "detail": str(exc)})
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
        elif item.get("seal_binding") is not None:
            raise ReceiptError(
                "SEAL_BINDING_MISMATCH",
                "%s falsely claims direct seal binding" % item.get("key"))
    return out


def write_shadow_receipt(output_root, date, seal_binding, objects,
                         verified_at, commit):
    """Write/reuse an immutable local-only receipt after complete verification."""
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
        "objects": [_public_object(o) for o in objects],
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
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=wc.ROOT,
            capture_output=True, text=True, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else "UNKNOWN"
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _default_output_root():
    return os.path.join(wc.ROOT, "work", "live", "canonical_receipts",
                        "shadow")


def _add_common(ap):
    ap.add_argument("--date", required=True)
    ap.add_argument("--bucket", default="kalshi-vault-ritcardo")
    ap.add_argument("--prefix", default="ec2")
    ap.add_argument("--raw-root")
    ap.add_argument("--warehouse-root")
    ap.add_argument("--quality-dir",
                    default=os.path.join(wc.ROOT, "work", "event_packs"))
    ap.add_argument("--output-root", default=_default_output_root())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="build the desired inventory only")
    _add_common(plan)
    shadow = sub.add_parser(
        "shadow", help="read and verify exact S3 versions; never write S3")
    _add_common(shadow)
    shadow.add_argument("--aws-cli", default="aws")
    shadow.add_argument("--metadata-only", action="store_true")
    shadow.add_argument("--probe-limit", type=int)
    args = ap.parse_args(argv)
    cfg = wc.load_config()
    raw_root = os.path.abspath(args.raw_root or cfg["raw_root"])
    warehouse_root = os.path.abspath(
        args.warehouse_root or cfg["warehouse_root"])
    quality_dir = os.path.abspath(args.quality_dir)
    output_root = os.path.abspath(args.output_root)
    checked_at = _now()

    try:
        seal_binding, objects = build_desired_inventory(
            args.date, args.bucket, args.prefix, raw_root, warehouse_root,
            quality_dir)
    except ReceiptError as exc:
        print("BLOCKED_INTEGRITY %s" % exc, file=sys.stderr)
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
    status_path = pathlib.Path(output_root) / ("date=%s" % args.date) / \
        "SHADOW_STATUS.json"
    write_atomic_json(status_path, status)
    print(json.dumps({"state": state, "status": str(status_path),
                      "receipt": str(receipt_path) if receipt_path else None,
                      "failures": len(failures)}, sort_keys=True))
    return (0 if state == "RECEIPT_VERIFIED_SHADOW"
            else 3 if state.startswith("PENDING") else 2)


if __name__ == "__main__":
    raise SystemExit(main())
