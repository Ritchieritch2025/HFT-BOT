#!/usr/bin/env python3
"""Forward-only canonical receipt preparation for new sealed dates.

This module complements :mod:`canonical_receipts`.  The older ``freeze-aux``
path is intentionally a migration path.  New dates require a producer-created
generation witness that binds the exact seal, catalog generation, dated dim
generation, and every original canonical S3 VersionId.  The research side
never reconstructs a generation from timestamps.  No catalog or dim copy is
created locally or in S3.

The commands in this file are S3 read-only.  They may create local auxiliary
bundles and local shadow receipts, but they never upload, tag, copy, or delete
an S3 object.  Durable receipt publication is a separate gated step.
"""
import argparse
import copy
import csv
import hashlib
import io
import json
import os
import pathlib
import shutil
import tempfile

import canonical_receipts as cr
import fresh_rfq_daily_eligibility as fresh_rfq_gate
import publication_generation as pg
import research_release
import warehouse_common as wc


FORWARD_AUX_SCHEMA = "canonical-forward-auxiliary-set-v3"
GENERATION_WITNESS_SCHEMA = "canonical-publication-generation-witness-v2"
GENERATION_WITNESS_STATE = "EXACT_GENERATION_WITNESS"
GENERATION_AUTHORITY_PRODUCER = "PRODUCER_COHERENT"
GENERATION_AUTHORITY_LEGACY = "LEGACY_MIGRATION_GENERATION"
LEGACY_MIGRATION_DATES = frozenset({
    "2026-07-10", "2026-07-11", "2026-07-15", "2026-07-16"})
HISTORICAL_METADATA_AUTHORITY_SCHEMA = (
    "canonical-historical-metadata-authority-v1")
HISTORICAL_METADATA_AUTHORITY_STATE = (
    "SEALED_METADATA_RECEIPTS_PENDING_EXACT_S3_BYTES")
MAX_HISTORICAL_RECEIPT_BYTES = 64 * 1024 * 1024
HISTORICAL_FACT_EXTENSIONS = {
    "orderbooks_l1": "parquet",
    "orderbooks_full": "parquet",
    "trades": "csv.gz",
}
LEGACY_MIGRATION_LIMITATIONS = (
    "HISTORICAL_MIGRATION_ONLY",
    "NOT_PRODUCER_GENERATION_COHERENT",
    "NO_CROSS_OBJECT_ATOMICITY_CLAIM",
    "CATALOG_DIM_COHERENCE_NOT_CLAIMED",
    "DATED_DIM_AND_SELECTED_CATALOG_ARE_MIGRATION_COMBINATION_ONLY",
)
MAX_LEGACY_CATALOG_BATCH_WINDOW_SECONDS = 15 * 60
FORWARD_VERSION_BINDING_SCHEMA = (
    "canonical-forward-generation-witness-binding-v2")
FORWARD_VERSION_BINDING_STATE = "EXACT_GENERATION_WITNESS_BOUND"
FORWARD_VERSION_SELECTION_RULE = (
    "CURRENT_EXACT_SHA256_ELSE_VERIFIED_MATCH_MAX_LASTMODIFIED_VERSIONID")
FORWARD_WITNESS_MEMBER_RESOLUTION = (
    "PRODUCER_GENERATION_WITNESS_EXACT_VERSION")
FORWARD_CATALOG_PROVENANCE = "PRODUCER_EXACT_GENERATION_WITNESS"
FORWARD_LEGACY_PROVENANCE = "LEGACY_MIGRATION_GENERATION_WITNESS"
FORWARD_LARGE_ATTESTATION = "LOCAL_LARGE_CANONICAL_ATTESTATION"
MAX_VERSION_BINDING_BYTES = 8 * 1024 * 1024
DAY_US = 86_400_000_000


def _historical_authority_blocked(date, reason, detail):
    raise cr.ReceiptError(
        "HISTORICAL_METADATA_AUTHORITY_BLOCKED",
        "date=%s reason=%s detail=%s" % (date, reason, detail))


def _historical_receipt(path, date, label):
    if not os.path.isfile(path):
        _historical_authority_blocked(date, "%s_MISSING" % label, path)
    try:
        payload, fingerprint = cr._freeze_file(
            path, max_bytes=MAX_HISTORICAL_RECEIPT_BYTES)
        value = json.loads(payload)
    except (OSError, UnicodeDecodeError, ValueError, cr.ReceiptError) as exc:
        _historical_authority_blocked(
            date, "%s_UNREADABLE" % label, str(exc))
    if not isinstance(value, dict):
        _historical_authority_blocked(
            date, "%s_INVALID" % label, "JSON root is not an object")
    return value, payload, fingerprint


def _historical_fact_attestation(path):
    """Hash once through a pinned fd and retain that exact fingerprint."""
    fd, before = cr._open_regular_nofollow(path)
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        after = cr._assert_fd_and_path_stable(fd, path, before, total)
    finally:
        os.close(fd)
    return after.st_size, digest.hexdigest(), cr._stat_key(after)


def _historical_assert_stable(date, label, path, fingerprint):
    try:
        cr._assert_fingerprint(path, fingerprint)
    except cr.ReceiptError as exc:
        _historical_authority_blocked(
            date, "%s_CHANGED" % label, str(exc))


def _historical_manifest_fact_projection(date, warehouse_root,
                                         manifest_rows, fact_proofs):
    """Require seal fact proofs to equal the canonical manifest file set."""
    facts_root = os.path.abspath(os.path.join(warehouse_root, "facts"))
    manifest_projection = []
    manifest_seen = set()
    for row in manifest_rows:
        table = row.get("table")
        extension = HISTORICAL_FACT_EXTENSIONS.get(table)
        if extension is None:
            _historical_authority_blocked(
                date, "MANIFEST_FACT_TABLE_INVALID", repr(table))
        category = row.get("category")
        subcategory = row.get("subcategory")
        category = None if category == "_unclassified" else category
        subcategory = None if subcategory == "_unclassified" else subcategory
        try:
            absolute = os.path.join(
                wc.partition_dir(
                    facts_root, table, category, subcategory, date),
                wc.partition_file(
                    table, category, subcategory, date, extension))
            rel = cr._safe_rel(
                os.path.relpath(absolute, facts_root).replace(os.sep, "/"),
                "historical manifest fact path")
        except (OSError, TypeError, ValueError, cr.ReceiptError) as exc:
            _historical_authority_blocked(
                date, "MANIFEST_FACT_PATH_INVALID", str(exc))
        identity = (rel, table)
        if identity in manifest_seen:
            _historical_authority_blocked(
                date, "MANIFEST_FACT_DUPLICATED", rel)
        manifest_seen.add(identity)
        manifest_projection.append({"file": rel, "table": table})

    seal_projection = []
    seal_seen = set()
    for proof in fact_proofs:
        try:
            identity = (
                cr._safe_rel(proof.get("file"), "seal archive file"),
                proof.get("table"))
        except cr.ReceiptError as exc:
            _historical_authority_blocked(
                date, "FACT_PROOF_INVALID", str(exc))
        if identity in seal_seen:
            _historical_authority_blocked(
                date, "FACT_PROOF_DUPLICATED", identity[0])
        seal_seen.add(identity)
        seal_projection.append({"file": identity[0], "table": identity[1]})

    if manifest_seen != seal_seen:
        missing = sorted(manifest_seen - seal_seen)
        extra = sorted(seal_seen - manifest_seen)
        _historical_authority_blocked(
            date, "MANIFEST_FACT_PROJECTION_MISMATCH",
            "missing=%s extra=%s" % (missing[:3], extra[:3]))
    return sorted(manifest_projection,
                  key=lambda row: (row["table"], row["file"]))


def _verify_historical_fact_bytes(date, warehouse_root, fact_proofs):
    """Hash every local fact named by the seal without consulting raw."""
    facts_root = os.path.abspath(os.path.join(warehouse_root, "facts"))
    seen = set()
    verified = []
    fingerprints = []
    for proof in sorted(fact_proofs, key=lambda row: row.get("file", "")):
        try:
            rel = cr._safe_rel(
                proof.get("file"), "historical seal archive file")
        except cr.ReceiptError as exc:
            _historical_authority_blocked(
                date, "FACT_PROOF_INVALID", str(exc))
        if rel in seen:
            _historical_authority_blocked(
                date, "FACT_PROOF_DUPLICATED", rel)
        seen.add(rel)
        path = os.path.abspath(os.path.join(facts_root, rel))
        try:
            if os.path.commonpath([facts_root, path]) != facts_root:
                _historical_authority_blocked(
                    date, "FACT_PATH_ESCAPE", rel)
            real_parent = os.path.realpath(os.path.dirname(path))
            if os.path.commonpath([facts_root, real_parent]) != facts_root:
                _historical_authority_blocked(
                    date, "FACT_PATH_ESCAPE", rel)
            observed_size, observed_sha, fingerprint = \
                _historical_fact_attestation(path)
        except (OSError, ValueError, cr.ReceiptError) as exc:
            _historical_authority_blocked(
                date, "FACT_BYTES_UNAVAILABLE", "%s: %s" % (rel, exc))
        if (observed_size != proof.get("size")
                or observed_sha != proof.get("sha256")):
            _historical_authority_blocked(
                date, "FACT_BYTES_MISMATCH",
                "%s expected=%s/%s observed=%s/%s" % (
                    rel, proof.get("size"), proof.get("sha256"),
                    observed_size, observed_sha))
        verified.append({
            "file": rel, "size": observed_size, "sha256": observed_sha,
            "table": proof.get("table"),
        })
        fingerprints.append((path, fingerprint))
    return verified, fingerprints


def historical_authoritative_day_inputs(date, raw_root, warehouse_root,
                                        quality_dir):
    """Authorize one bounded legacy day after local raw has been pruned.

    Only the four enumerated migration dates may use this path.  The full-v2
    seal, manifest projection, and every local fact byte remain mandatory.
    Raw bytes are never opened or reconstructed: the seal's raw metadata is
    paired with exact-inventory capture/L2 receipts here, and the later
    durability pass must verify every named S3 VersionId by size and SHA-256.
    """
    cr._validate_date(date)
    if date not in LEGACY_MIGRATION_DATES:
        _historical_authority_blocked(
            date, "DATE_NOT_ALLOWLISTED",
            "historical authority is restricted to the four legacy dates")
    try:
        result = cr._authoritative_day_inputs(
            date, raw_root, warehouse_root)
    except cr.ReceiptError as exc:
        _historical_authority_blocked(
            date, "SEALED_METADATA_INVALID", str(exc))
    (seal, binding, manifest_payload, manifest_digest, manifest_rows,
     raw_proofs, fact_proofs) = result

    manifest_fact_projection = _historical_manifest_fact_projection(
        date, warehouse_root, manifest_rows, fact_proofs)
    # Do not rely on seal-time inode shortcuts for the retained facts.  The
    # historical authority re-hashes all local fact bytes now.
    verified_facts, fact_fingerprints = _verify_historical_fact_bytes(
        date, warehouse_root, fact_proofs)

    capture_path = os.path.join(
        quality_dir, "capture_gap_receipt_%s.json" % date)
    capture, capture_payload, capture_fingerprint = _historical_receipt(
        capture_path, date, "CAPTURE_RECEIPT")
    valid, reason = cr._validate_capture_receipt(capture, seal, date)
    if not valid:
        _historical_authority_blocked(
            date, "CAPTURE_RECEIPT_INVALID", reason)

    dated_l2 = [
        row for row in raw_proofs
        if row["file"].startswith("date=%s/" % date)
        and os.path.basename(row["file"]).startswith("l2_")]
    l2_binding = None
    l2_fingerprint = None
    l2_path = os.path.join(quality_dir, "l2_gaps_%s.json" % date)
    has_l2_facts = any(proof.get("table") == "orderbooks_full"
                       for proof in fact_proofs)
    if has_l2_facts and not dated_l2:
        _historical_authority_blocked(
            date, "L2_SEAL_SOURCE_MISSING",
            "orderbooks_full facts exist without sealed dated L2 raw proof")
    l2_required = (
        bool(dated_l2) or has_l2_facts)
    if l2_required:
        l2, l2_payload, l2_fingerprint = _historical_receipt(
            l2_path, date, "L2_RECEIPT")
        inventory = l2.get("file_inventory")
        inventory_valid = isinstance(inventory, list)
        inventory_files = []
        if inventory_valid:
            for row in inventory:
                if (not isinstance(row, dict)
                        or not isinstance(row.get("file"), str)
                        or not row["file"]
                        or not isinstance(row.get("bytes"), int)
                        or isinstance(row["bytes"], bool)
                        or row["bytes"] < 0):
                    inventory_valid = False
                    break
                inventory_files.append(row["file"])
        if (not inventory_valid
                or len(set(inventory_files)) != len(inventory_files)):
            _historical_authority_blocked(
                date, "L2_RECEIPT_INVALID",
                "file_inventory is malformed or duplicated")
        try:
            quality, reason = research_release.validate_l2_receipt(
                l2, seal, date)
        except Exception as exc:
            _historical_authority_blocked(
                date, "L2_RECEIPT_INVALID", str(exc))
        if quality is None:
            _historical_authority_blocked(
                date, "L2_RECEIPT_INVALID", reason)
        l2_binding = {
            "size": len(l2_payload),
            "sha256": hashlib.sha256(l2_payload).hexdigest(),
            "sealed_inventory_sha256": cr.canonical_sha256(sorted(
                ({"file": row["file"], "size": row["size"]}
                 for row in dated_l2), key=lambda row: row["file"])),
        }

    authority = {
        "schema_version": HISTORICAL_METADATA_AUTHORITY_SCHEMA,
        "state": HISTORICAL_METADATA_AUTHORITY_STATE,
        "date": date,
        "seal_sha256": binding["sha256"],
        "manifest_date_sha256": manifest_digest,
        "raw_files_sha256": cr.canonical_sha256(raw_proofs),
        "archive_file_stats_sha256": cr.canonical_sha256(fact_proofs),
        "manifest_fact_projection_sha256":
            cr.canonical_sha256(manifest_fact_projection),
        "verified_fact_bytes_sha256": cr.canonical_sha256(verified_facts),
        "capture_receipt": {
            "size": len(capture_payload),
            "sha256": hashlib.sha256(capture_payload).hexdigest(),
            "sealed_inventory_sha256": cr.canonical_sha256(sorted(
                ({"file": row["file"], "size": row["size"]}
                 for row in raw_proofs
                 if row["file"].startswith("date=%s/" % date)
                 and os.path.basename(row["file"]).startswith("firehose_")),
                key=lambda row: row["file"])),
        },
        "l2_receipt": l2_binding,
        "local_raw_bytes_read": 0,
        "raw_rebuild_or_download": False,
        "required_followup": "EXACT_S3_VERSION_SIZE_AND_SHA256_VERIFICATION",
    }
    binding["historical_metadata_authority"] = authority
    _historical_assert_stable(
        date, "SEAL", binding["local_path"],
        binding["_source_fingerprint"])
    manifest_path = os.path.join(warehouse_root, "manifest.csv")
    try:
        current_manifest, manifest_fingerprint = cr._freeze_file(
            manifest_path)
    except cr.ReceiptError as exc:
        _historical_authority_blocked(
            date, "MANIFEST_CHANGED", str(exc))
    if current_manifest != manifest_payload:
        _historical_authority_blocked(
            date, "MANIFEST_CHANGED", manifest_path)
    _historical_assert_stable(
        date, "MANIFEST", manifest_path, manifest_fingerprint)
    _historical_assert_stable(
        date, "CAPTURE_RECEIPT", capture_path, capture_fingerprint)
    if l2_fingerprint is not None:
        _historical_assert_stable(
            date, "L2_RECEIPT", l2_path, l2_fingerprint)
    for fact_path, fact_fingerprint in fact_fingerprints:
        _historical_assert_stable(
            date, "FACT_BYTES", fact_path, fact_fingerprint)
    return (seal, binding, manifest_payload, manifest_digest, manifest_rows,
            raw_proofs, fact_proofs)


def _forward_authoritative_day_inputs(date, raw_root, warehouse_root,
                                      quality_dir):
    if date in LEGACY_MIGRATION_DATES:
        return historical_authoritative_day_inputs(
            date, raw_root, warehouse_root, quality_dir)
    return cr._authoritative_day_inputs(date, raw_root, warehouse_root)


def generation_witness_prefix(prefix, date):
    cr._validate_date(date)
    return cr._join_key(
        prefix, "control", "publication-generations", "v1",
        "date=%s" % date)


def expected_generation_witness_key(prefix, date, payload_sha256):
    if not isinstance(payload_sha256, str) or not cr.SHA256_RE.match(
            payload_sha256):
        raise cr.ReceiptError(
            "GENERATION_WITNESS_INVALID", "witness sha256 invalid")
    return cr._join_key(
        generation_witness_prefix(prefix, date),
        "witness-%s.json" % payload_sha256)


def _generation_witness_contract(seal_binding, date, bucket, prefix):
    allowed = [GENERATION_AUTHORITY_PRODUCER]
    if date in LEGACY_MIGRATION_DATES:
        allowed.append(GENERATION_AUTHORITY_LEGACY)
    return {
        "schema_version": GENERATION_WITNESS_SCHEMA,
        "state": GENERATION_WITNESS_STATE,
        "date": date,
        "bucket": bucket,
        "key_prefix": generation_witness_prefix(prefix, date) + "/",
        "seal_sha256": seal_binding["sha256"],
        "seal_sealed_at_utc": cr._canonical_utc(
            seal_binding["sealed_at"], "seal sealed_at"),
        "allowed_generation_authorities": sorted(allowed),
        "discovery_rule": "EXACTLY_ONE_CONTENT_ADDRESSED_WITNESS",
        "read_rule": "HEAD_VERSIONID_THEN_EXACT_GET_NO_FALLBACK",
    }


def _exact_s3_binding(raw, label):
    if not isinstance(raw, dict) or set(raw) != {
            "bucket", "key", "VersionId", "size", "sha256",
            "LastModified"}:
        raise cr.ReceiptError(
            "GENERATION_WITNESS_INVALID", "%s binding shape invalid" % label)
    bucket = raw["bucket"]
    key = cr._safe_rel(raw["key"], "%s key" % label)
    version_id = raw["VersionId"]
    if (not isinstance(bucket, str) or not bucket or "/" in bucket
            or not isinstance(version_id, str) or not version_id.strip()
            or version_id.lower() == "null"):
        raise cr.ReceiptError(
            "GENERATION_WITNESS_INVALID", "%s exact version invalid" % label)
    cr._check_expected(raw["size"], raw["sha256"], label)
    modified = cr._canonical_utc(
        raw["LastModified"], "%s LastModified" % label)
    return dict(raw, key=key, LastModified=modified)


def _embedded_generation(raw, group, date, expected_paths):
    try:
        manifest = pg.validate_manifest(
            json.dumps(raw, sort_keys=True), ".", group=group, date=date,
            expected_paths=expected_paths, verify_files=False)
    except (pg.GenerationError, TypeError, ValueError) as exc:
        raise cr.ReceiptError(
            "GENERATION_WITNESS_INVALID",
            "%s generation invalid: %s" % (group, exc))
    return manifest


def legacy_dim_unbound_source_id(date, bucket, prefix, dim_files):
    """Derive the only permitted legacy dim source id without claiming linkage."""
    return cr.canonical_sha256({
        "schema_version": "legacy-dated-dim-unbound-source-v1",
        "date": date, "bucket": bucket, "prefix": prefix,
        "dim_files": copy.deepcopy(dim_files),
        "catalog_dim_coherence_claim": False,
    })


def validate_generation_witness(payload, date, bucket, prefix,
                                seal_sha256, seal_size,
                                seal_sealed_at_utc=None):
    """Validate one producer or explicitly bounded legacy witness payload."""
    if not isinstance(payload, dict) or set(payload) != {
            "schema_version", "state", "generation_authority", "date",
            "bucket", "prefix", "seal", "catalog_generation",
            "dim_generation", "catalog_dim_coherence_claim",
            "legacy_migration", "objects",
            "object_set_sha256"}:
        raise cr.ReceiptError(
            "GENERATION_WITNESS_INVALID", "witness shape invalid")
    authority = payload["generation_authority"]
    coherence_claim = payload["catalog_dim_coherence_claim"]
    if (payload["schema_version"] != GENERATION_WITNESS_SCHEMA
            or payload["state"] != GENERATION_WITNESS_STATE
            or payload["date"] != date or payload["bucket"] != bucket
            or payload["prefix"] != prefix
            or authority not in {
                GENERATION_AUTHORITY_PRODUCER,
                GENERATION_AUTHORITY_LEGACY}):
        raise cr.ReceiptError(
            "GENERATION_WITNESS_INVALID", "witness authority mismatch")
    if authority == GENERATION_AUTHORITY_LEGACY:
        if coherence_claim is not False:
            raise cr.ReceiptError(
                "GENERATION_WITNESS_INVALID",
                "legacy witness must deny catalog/dim coherence")
        if date not in LEGACY_MIGRATION_DATES:
            raise cr.ReceiptError(
                "GENERATION_WITNESS_INVALID",
                "legacy migration is forbidden for this date")
        migration = payload["legacy_migration"]
        if not isinstance(migration, dict) or set(migration) != {
                "selection_authority", "seal_sealed_at_utc",
                "catalog_batch_start_utc", "catalog_batch_end_utc",
                "selection_rule", "dim_selection_rule",
                "catalog_history_proof_sha256", "limitations"}:
            raise cr.ReceiptError(
                "GENERATION_WITNESS_INVALID", "legacy migration shape invalid")
        if (migration["selection_authority"]
                != "FIRST_POST_SEAL_CATALOG_FULL_SYNC_BATCH"
                or migration["selection_rule"]
                != "UNIQUE_EARLIEST_COMPLETE_CATALOG_BATCH_AFTER_SEAL"
                or migration["dim_selection_rule"] != "DATED_EXACT_VERSION"
                or not isinstance(
                    migration["catalog_history_proof_sha256"], str)
                or not cr.SHA256_RE.match(
                    migration["catalog_history_proof_sha256"])
                or migration["limitations"]
                != list(LEGACY_MIGRATION_LIMITATIONS)):
            raise cr.ReceiptError(
                "GENERATION_WITNESS_INVALID", "legacy migration claim invalid")
        sealed_at = cr._canonical_utc(
            migration["seal_sealed_at_utc"], "legacy seal sealed_at")
        batch_start = cr._canonical_utc(
            migration["catalog_batch_start_utc"], "catalog batch start")
        batch_end = cr._canonical_utc(
            migration["catalog_batch_end_utc"], "catalog batch end")
        start_dt = cr._parse_utc(batch_start, "catalog batch start")
        end_dt = cr._parse_utc(batch_end, "catalog batch end")
        sealed_dt = cr._parse_utc(sealed_at, "legacy seal sealed_at")
        if (seal_sealed_at_utc is not None
                and sealed_at != cr._canonical_utc(
                    seal_sealed_at_utc, "expected seal sealed_at")):
            raise cr.ReceiptError(
                "GENERATION_WITNESS_INVALID",
                "legacy witness binds a different seal time")
        if (start_dt <= sealed_dt or end_dt < start_dt
                or (end_dt - start_dt).total_seconds()
                > MAX_LEGACY_CATALOG_BATCH_WINDOW_SECONDS):
            raise cr.ReceiptError(
                "GENERATION_WITNESS_INVALID",
                "legacy catalog batch window invalid")
    else:
        if (payload["legacy_migration"] is not None
                or coherence_claim is not True):
            raise cr.ReceiptError(
                "GENERATION_WITNESS_INVALID",
                "producer coherence claim is invalid")
        sealed_at = batch_start = batch_end = None

    seal = _exact_s3_binding(payload["seal"], "seal")
    expected_seal_key = cr._join_key(
        prefix, "warehouse", "seals", "date=%s.json" % date)
    if (seal["bucket"] != bucket or seal["key"] != expected_seal_key
            or seal["sha256"] != seal_sha256 or seal["size"] != seal_size):
        raise cr.ReceiptError(
            "GENERATION_WITNESS_INVALID", "seal exact binding mismatch")

    catalog_paths = {
        "catalog/" + rel for rel in cr.CATALOG_REQUIRED + cr.CATALOG_OPTIONAL}
    raw_catalog_files = (payload["catalog_generation"].get("files")
                         if isinstance(payload["catalog_generation"], dict)
                         else None)
    if not isinstance(raw_catalog_files, list):
        raise cr.ReceiptError(
            "GENERATION_WITNESS_INVALID", "catalog generation files missing")
    observed_catalog_paths = {
        row.get("relative_path") for row in raw_catalog_files
        if isinstance(row, dict)}
    required_catalog_paths = {"catalog/" + rel for rel in cr.CATALOG_REQUIRED}
    if (not required_catalog_paths <= observed_catalog_paths
            or not observed_catalog_paths <= catalog_paths
            or len(observed_catalog_paths) != len(raw_catalog_files)):
        raise cr.ReceiptError(
            "GENERATION_WITNESS_INVALID", "catalog generation set invalid")
    catalog = _embedded_generation(
        payload["catalog_generation"], "catalog", None,
        observed_catalog_paths)
    dim_paths = {
        "dim/snapshots/date=%s/%s" % (date, name)
        for name in cr.DIM_REQUIRED}
    dim = _embedded_generation(
        payload["dim_generation"], "dim", date, dim_paths)
    if authority == GENERATION_AUTHORITY_PRODUCER:
        if dim["source_catalog_generation_id"] != catalog["generation_id"]:
            raise cr.ReceiptError(
                "GENERATION_WITNESS_INVALID",
                "dim generation does not bind catalog generation")
    else:
        expected_unbound = legacy_dim_unbound_source_id(
            date, bucket, prefix, dim["files"])
        if (dim["source_catalog_generation_id"] != expected_unbound
                or dim["source_catalog_generation_id"]
                == catalog["generation_id"]):
            raise cr.ReceiptError(
                "GENERATION_WITNESS_INVALID",
                "legacy dim falsely claims selected catalog coherence")

    expected = {}
    for row in catalog["files"]:
        rel = row["relative_path"][len("catalog/"):]
        logical = cr._join_key("warehouse", "catalog", rel)
        expected[logical] = {
            "bucket": bucket,
            "key": cr._join_key(prefix, logical),
            "size": row["size"], "sha256": row["sha256"],
            "source_kind": "catalog", "family": "catalog_at_publication",
            "table": rel.split("/", 1)[0], "channel": None,
        }
    dim_prefix = "dim/snapshots/date=%s/" % date
    for row in dim["files"]:
        name = row["relative_path"][len(dim_prefix):]
        logical = cr._join_key(
            "warehouse", "dim", "snapshots", "date=%s" % date, name)
        expected[logical] = {
            "bucket": bucket,
            "key": cr._join_key(prefix, logical),
            "size": row["size"], "sha256": row["sha256"],
            "source_kind": "dim_snapshot", "family": "dim_snapshot",
            "table": name.rsplit(".", 1)[0], "channel": None,
        }

    raw_objects = payload["objects"]
    if not isinstance(raw_objects, list):
        raise cr.ReceiptError(
            "GENERATION_WITNESS_INVALID", "witness objects missing")
    normalized, seen_logical, seen_physical = [], set(), set()
    for raw in raw_objects:
        if not isinstance(raw, dict) or set(raw) != {
                "logical_source_key", "bucket", "key", "VersionId", "size",
                "sha256", "LastModified"}:
            raise cr.ReceiptError(
                "GENERATION_WITNESS_INVALID", "witness member shape invalid")
        logical = cr._safe_rel(
            raw["logical_source_key"], "witness logical source key")
        metadata = expected.get(logical)
        exact = _exact_s3_binding({
            name: raw[name] for name in (
                "bucket", "key", "VersionId", "size", "sha256",
                "LastModified")}, logical)
        physical = (exact["bucket"], exact["key"], exact["VersionId"])
        if (logical in seen_logical or physical in seen_physical
                or metadata is None
                or any(exact[name] != metadata[name]
                       for name in ("bucket", "key", "size", "sha256"))):
            raise cr.ReceiptError(
                "GENERATION_WITNESS_INVALID", "witness member mismatch")
        if (authority == GENERATION_AUTHORITY_LEGACY
                and metadata["family"] == "catalog_at_publication"
                and not (cr._parse_utc(batch_start, "catalog batch start")
                         <= cr._parse_utc(
                             exact["LastModified"], "member LastModified")
                         <= cr._parse_utc(batch_end, "catalog batch end"))):
            raise cr.ReceiptError(
                "GENERATION_WITNESS_INVALID",
                "legacy catalog member is outside first complete batch")
        seen_logical.add(logical)
        seen_physical.add(physical)
        normalized.append(dict(
            logical_source_key=logical, **exact, **{
                name: metadata[name] for name in (
                    "source_kind", "family", "table", "channel")}))
    normalized.sort(key=lambda row: row["logical_source_key"])
    if seen_logical != set(expected) or raw_objects != [{
            name: row[name] for name in (
                "logical_source_key", "bucket", "key", "VersionId", "size",
                "sha256", "LastModified")}
            for row in normalized]:
        raise cr.ReceiptError(
            "GENERATION_WITNESS_INVALID", "witness member set incomplete")
    if payload["object_set_sha256"] != cr.canonical_sha256(raw_objects):
        raise cr.ReceiptError(
            "GENERATION_WITNESS_INVALID", "witness object digest mismatch")
    return {
        "generation_authority": authority,
        "catalog_dim_coherence_claim": coherence_claim,
        "seal": seal,
        "catalog_generation": catalog,
        "dim_generation": dim,
        "objects": normalized,
        "legacy_migration": copy.deepcopy(payload["legacy_migration"]),
    }


def generation_witness_bytes(payload):
    """The only valid S3 wire representation for a generation witness."""
    return cr.canonical_bytes(payload)


def generation_witness_artifact(payload):
    """Return the content-addressed key and canonical bytes; performs no I/O."""
    raw = generation_witness_bytes(payload)
    digest = hashlib.sha256(raw).hexdigest()
    return expected_generation_witness_key(
        payload["prefix"], payload["date"], digest), raw


def build_generation_witness_payload(
        date, bucket, prefix, seal, catalog_generation, dim_generation,
        objects, *, generation_authority=GENERATION_AUTHORITY_PRODUCER,
        legacy_migration=None, catalog_dim_coherence_claim=None):
    """Pure producer helper; performs no local or AWS write."""
    projection = {
        "schema_version": GENERATION_WITNESS_SCHEMA,
        "state": GENERATION_WITNESS_STATE,
        "generation_authority": generation_authority,
        "date": date, "bucket": bucket, "prefix": prefix,
        "seal": copy.deepcopy(seal),
        "catalog_generation": copy.deepcopy(catalog_generation),
        "dim_generation": copy.deepcopy(dim_generation),
        "catalog_dim_coherence_claim": (
            generation_authority == GENERATION_AUTHORITY_PRODUCER
            if catalog_dim_coherence_claim is None
            else catalog_dim_coherence_claim),
        "legacy_migration": copy.deepcopy(legacy_migration),
        "objects": copy.deepcopy(objects),
    }
    projection["object_set_sha256"] = cr.canonical_sha256(
        projection["objects"])
    validated = validate_generation_witness(
        projection, date, bucket, prefix, seal["sha256"], seal["size"],
        (legacy_migration or {}).get("seal_sealed_at_utc"))
    if len(validated["objects"]) != len(objects):
        raise cr.ReceiptError(
            "GENERATION_WITNESS_INVALID", "witness construction incomplete")
    return projection


def _stage_local(pending, rel, payload, **metadata):
    return cr._aux_local_object(pending, rel, payload, **metadata)


def expected_control_key(row, prefix, date):
    """Derive, never trust, the only S3 key allowed for a small control."""
    digest = row["sha256"]
    kind = row.get("source_kind")
    if kind == "warehouse_manifest_day":
        return cr._join_key(
            prefix, "warehouse", "publication-snapshots", "v1",
            "date=%s" % date, "manifest", "sha256=%s" % digest,
            "manifest.csv")
    if kind == "corrections_ledger_day":
        return cr._join_key(
            prefix, "warehouse", "publication-snapshots", "v1",
            "date=%s" % date, "corrections", "ledger_day",
            "sha256=%s" % digest, "ledger_day.ndjson")
    controls = {
        "capture_gap_receipt": ("capture_gap_receipt",
                                "capture_gap_receipt.json"),
        "capture_gaps_projection": ("capture_gaps", "capture_gaps.csv"),
        "l2_quality_receipt": ("l2_gaps", "l2_gaps.json"),
    }
    if kind in controls:
        directory, filename = controls[kind]
        return cr._join_key(
            prefix, "control", "quality", "v1", "date=%s" % date,
            directory, "sha256=%s" % digest, filename)
    raise cr.ReceiptError(
        "CONTROL_KEY_FORBIDDEN", "unsupported small control kind %r" % kind)


def _strict_day_gap_intervals(path, date):
    """Read the source CSV without silently discarding malformed rows."""
    if not os.path.isfile(path):
        return None
    payload, _fingerprint = cr._freeze_file(path)
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8")))
        rows = list(reader)
    except (UnicodeDecodeError, csv.Error) as exc:
        raise cr.ReceiptError("QUALITY_INVALID", str(exc))
    if reader.fieldnames != ["start_us", "end_us"]:
        raise cr.ReceiptError(
            "QUALITY_INVALID", "capture gap columns invalid")
    lo, hi = wc.day_start_us(date), wc.day_start_us(date) + DAY_US
    selected = []
    previous_end = None
    for lineno, row in enumerate(rows, 2):
        if set(row) != {"start_us", "end_us"} or None in row:
            raise cr.ReceiptError(
                "QUALITY_INVALID", "capture gap row %d is malformed" % lineno)
        try:
            start, end = int(row["start_us"]), int(row["end_us"])
        except (TypeError, ValueError):
            raise cr.ReceiptError(
                "QUALITY_INVALID", "capture gap row %d is malformed" % lineno)
        if start >= end:
            raise cr.ReceiptError(
                "QUALITY_INVALID", "capture gap row %d is inverted" % lineno)
        if start < hi and end > lo:
            if not (lo <= start < end <= hi):
                raise cr.ReceiptError(
                    "QUALITY_INVALID",
                    "capture gap row %d crosses the date boundary" % lineno)
            if previous_end is not None and start < previous_end:
                raise cr.ReceiptError(
                    "QUALITY_INVALID",
                    "capture gap rows overlap or are unsorted")
            selected.append({"start_us": start, "end_us": end})
            previous_end = end
    return selected


def freeze_forward_auxiliary_set(date, bucket, prefix, raw_root,
                                 warehouse_root, quality_dir, aux_root):
    """Freeze small controls and require an exact generation witness.

    Catalog and dated dim are deliberately not read or copied locally.  Their
    original canonical keys and exact versions must be supplied by a producer
    generation witness.  There is no timestamp-based research-side fallback.
    """
    cr._validate_date(date)
    if not bucket or "/" in bucket:
        raise cr.ReceiptError("INVALID_BUCKET", repr(bucket))
    prefix = cr._safe_rel(prefix.strip("/"), "canonical prefix")
    (seal, binding, manifest_payload, manifest_digest, _manifest_rows,
     raw_proofs, fact_proofs) = _forward_authoritative_day_inputs(
         date, raw_root, warehouse_root, quality_dir)
    seal_sha = binding["sha256"]
    witness_contract = _generation_witness_contract(
        dict(binding, sealed_at=seal.get("sealed_at")),
        date, bucket, prefix)

    date_root = pathlib.Path(aux_root) / ("date=%s" % date)
    date_root.mkdir(parents=True, exist_ok=True)
    pending = tempfile.mkdtemp(prefix=".pending-", dir=str(date_root))
    objects = []
    try:
        day_manifest, day_digest, _day_rows = cr._manifest_day_csv(
            manifest_payload, date)
        if day_digest != manifest_digest:
            raise cr.ReceiptError(
                "MANIFEST_DATE_PROJECTION_MISMATCH",
                "serialized day projection changed semantics")
        manifest_sha = hashlib.sha256(day_manifest).hexdigest()
        objects.append(_stage_local(
            pending, "manifest/manifest.csv", day_manifest,
            bucket=bucket,
            key=cr._join_key(
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

        late_path = os.path.join(
            warehouse_root, "corrections", "date=%s" % date,
            "late_rows.ndjson")
        ledger_path = os.path.join(
            warehouse_root, "corrections", "ledger.ndjson")
        late_present = os.path.lexists(late_path)
        ledger_present = os.path.lexists(ledger_path)
        if ledger_present:
            ledger_day, ledger_rows, ledger_fingerprint = (
                cr._ledger_projection_file(ledger_path, date))
        else:
            ledger_day, ledger_rows, ledger_fingerprint = b"", [], None
        if (late_present != bool(ledger_rows)):
            raise cr.ReceiptError(
                "CORRECTIONS_INVALID",
                "late_rows and date ledger do not agree")
        if late_present:
            (late_size, late_sha, late_validation,
             late_fingerprint) = cr._late_correction_file_attestation(
                 late_path, ledger_rows, date)
            ledger_sha = hashlib.sha256(ledger_day).hexdigest()
            corr_evidence = {
                "ledger_day_sha256": ledger_sha,
                "late_rows_validation": late_validation,
            }
            objects.append({
                "storage_mode": FORWARD_LARGE_ATTESTATION,
                "local_relpath": None,
                "bucket": bucket,
                "key": cr._join_key(
                    prefix, "warehouse", "corrections",
                    "date=%s" % date, "late_rows.ndjson"),
                "logical_source_key": cr._join_key(
                    "warehouse", "corrections", "date=%s" % date,
                    "late_rows.ndjson"),
                "source_kind": "correction",
                "family": "corrections_at_cutoff",
                "table": None,
                "channel": None,
                "date": date,
                "size": late_size,
                "sha256": late_sha,
                "seal_binding": seal_sha,
                "evidence_binding": corr_evidence,
                "mutable_source": False,
                "required": True,
                "durability_scope": True,
                "research_candidate": True,
                "exposure_policy": "PENDING_ELIGIBILITY_TAG",
                "version_resolution": "FORWARD_CURRENT_EXACT",
                "canonical_source":
                    "LOCAL_LARGE_ATTESTATION_THEN_EXACT_S3_VERSION",
                "expected_version_id": None,
            })
            objects.append(_stage_local(
                pending, "corrections/ledger_day.ndjson", ledger_day,
                bucket=bucket,
                key=cr._join_key(
                    prefix, "warehouse", "publication-snapshots", "v1",
                    "date=%s" % date, "corrections", "ledger_day",
                    "sha256=%s" % ledger_sha, "ledger_day.ndjson"),
                logical_source_key=cr._join_key(
                    "warehouse", "corrections", "date=%s" % date,
                    "ledger_day.ndjson"),
                source_kind="corrections_ledger_day",
                family="corrections_at_cutoff",
                table=None, channel=None, date=date, seal_binding=seal_sha,
                evidence_binding=corr_evidence, research_candidate=True,
                exposure_policy="PENDING_ELIGIBILITY_TAG",
                version_resolution="WRITE_ONCE_CONTENT_ADDRESSED_EXACT",
                canonical_source="PLANNED_DATE_SCOPED_CONTROL_SYNC"))
            cr._assert_fingerprint(ledger_path, ledger_fingerprint)
            cr._assert_fingerprint(late_path, late_fingerprint)

        capture_path = os.path.join(
            quality_dir, "capture_gap_receipt_%s.json" % date)
        if not os.path.isfile(capture_path):
            raise cr.ReceiptError(
                "AUX_INPUT_INCOMPLETE", "capture gap receipt is missing")
        capture_payload = cr._freeze_file(capture_path)[0]
        try:
            capture_receipt = json.loads(capture_payload)
        except ValueError as exc:
            raise cr.ReceiptError("QUALITY_INVALID", str(exc))
        capture_valid, capture_reason = cr._validate_capture_receipt(
            capture_receipt, seal, date)
        if not capture_valid:
            raise cr.ReceiptError("QUALITY_INVALID", capture_reason)
        receipt_gaps = _capture_gaps(capture_receipt, date)
        objects.append(_stage_local(
            pending, "quality/capture_gap_receipt.json", capture_payload,
            bucket=bucket,
            key=cr._join_key(
                prefix, "control", "quality", "v1", "date=%s" % date,
                "capture_gap_receipt",
                "sha256=%s" % hashlib.sha256(capture_payload).hexdigest(),
                "capture_gap_receipt.json"),
            logical_source_key=cr._join_key(
                "control", "quality", "v1", "date=%s" % date,
                "capture_gap_receipt.json"),
            source_kind="capture_gap_receipt", family="capture_gap_receipt",
            table=None, channel=None, date=date, seal_binding=seal_sha,
            evidence_binding="sealed firehose inventory",
            research_candidate=True,
            exposure_policy="PENDING_ELIGIBILITY_TAG",
            version_resolution="WRITE_ONCE_CONTENT_ADDRESSED_EXACT",
            canonical_source="PLANNED_SEALED_DAY_CONTROL_SYNC"))

        gaps = _strict_day_gap_intervals(
            os.path.join(quality_dir, "capture_gaps.csv"), date)
        if gaps is None and receipt_gaps:
            raise cr.ReceiptError(
                "QUALITY_INVALID", "capture gap projection is missing")
        if gaps is not None and gaps != receipt_gaps:
            raise cr.ReceiptError(
                "QUALITY_INVALID",
                "capture gap projection/receipt mismatch")
        if gaps is not None:
            lines = ["start_us,end_us"] + [
                "%d,%d" % (row["start_us"], row["end_us"])
                for row in gaps]
            gap_payload = ("\n".join(lines) + "\n").encode("utf-8")
            objects.append(_stage_local(
                pending, "quality/capture_gaps.csv", gap_payload,
                bucket=bucket,
                key=cr._join_key(
                    prefix, "control", "quality", "v1", "date=%s" % date,
                    "capture_gaps",
                    "sha256=%s" % hashlib.sha256(gap_payload).hexdigest(),
                    "capture_gaps.csv"),
                logical_source_key=cr._join_key(
                    "control", "quality", "v1", "date=%s" % date,
                    "capture_gaps.csv"),
                source_kind="capture_gaps_projection",
                family="capture_gaps_projection", table=None, channel=None,
                date=date, seal_binding=seal_sha,
                evidence_binding="capture receipt gap intervals",
                research_candidate=True,
                exposure_policy="PENDING_ELIGIBILITY_TAG",
                version_resolution="WRITE_ONCE_CONTENT_ADDRESSED_EXACT",
                canonical_source="PLANNED_SEALED_DAY_CONTROL_SYNC"))

        l2_required = (
            any(proof.get("table") == "orderbooks_full"
                for proof in fact_proofs)
            or any(
                proof["file"].startswith("date=%s/" % date)
                and os.path.basename(proof["file"]).startswith("l2_")
                for proof in raw_proofs))
        l2_path = os.path.join(quality_dir, "l2_gaps_%s.json" % date)
        if l2_required and not os.path.isfile(l2_path):
            raise cr.ReceiptError(
                "AUX_INPUT_INCOMPLETE", "L2 quality receipt is missing")
        if os.path.isfile(l2_path) and (
                l2_required or date not in LEGACY_MIGRATION_DATES):
            l2_payload = cr._freeze_file(l2_path)[0]
            try:
                l2_receipt = json.loads(l2_payload)
                l2_quality, l2_reason = research_release.validate_l2_receipt(
                    l2_receipt, seal, date)
            except Exception as exc:
                raise cr.ReceiptError("QUALITY_INVALID", str(exc))
            if l2_quality is None:
                raise cr.ReceiptError("QUALITY_INVALID", l2_reason)
            objects.append(_stage_local(
                pending, "quality/l2_gaps.json", l2_payload,
                bucket=bucket,
                key=cr._join_key(
                    prefix, "control", "quality", "v1", "date=%s" % date,
                    "l2_gaps",
                    "sha256=%s" % hashlib.sha256(l2_payload).hexdigest(),
                    "l2_gaps.json"),
                logical_source_key=cr._join_key(
                    "control", "quality", "v1", "date=%s" % date,
                    "l2_gaps.json"),
                source_kind="l2_quality_receipt", family="l2_quality",
                table=None, channel=None, date=date, seal_binding=seal_sha,
                evidence_binding="sealed l2 inventory",
                research_candidate=True,
                exposure_policy="PENDING_ELIGIBILITY_TAG",
                version_resolution="WRITE_ONCE_CONTENT_ADDRESSED_EXACT",
                canonical_source="PLANNED_SEALED_DAY_CONTROL_SYNC"))

        for row in objects:
            row.setdefault("required", True)
            row.setdefault("durability_scope", True)
        objects.sort(key=lambda row: row["logical_source_key"])
        desired_projection = [{
            "bucket": row["bucket"],
            "key": row["key"],
            "logical_source_key": row["logical_source_key"],
            "source_kind": row["source_kind"],
            "family": row["family"],
            "date": row["date"],
            "size": row["size"],
            "sha256": row["sha256"],
            "seal_binding": row["seal_binding"],
            "evidence_binding": row["evidence_binding"],
            "required": row["required"],
            "durability_scope": row["durability_scope"],
            "research_candidate": row["research_candidate"],
            "exposure_policy": row["exposure_policy"],
            "version_resolution": row["version_resolution"],
            "canonical_source": row["canonical_source"],
        } for row in objects]
        desired_projection.sort(key=lambda row: (
            row["logical_source_key"], row["bucket"], row["key"]))
        desired_set_sha = cr.canonical_sha256(desired_projection)
        projection = {
            "schema_version": FORWARD_AUX_SCHEMA,
            "mode": "FORWARD_CANONICAL_CAPTURE",
            "date": date,
            "bucket": bucket,
            "prefix": prefix,
            "seal_sha256": seal_sha,
            "manifest_date_sha256": manifest_digest,
            "desired_set_sha256": desired_set_sha,
            "generation_witness_contract": witness_contract,
            "objects": objects,
        }
        aux_sha = cr.canonical_sha256(projection)
        descriptor = dict(projection, aux_set_sha256=aux_sha)
        with open(os.path.join(pending, "AUX_SET.json"), "x") as handle:
            json.dump(descriptor, handle, sort_keys=True, indent=2)
            handle.write("\n")
        final = date_root / ("aux-set=%s" % aux_sha)
        if final.exists():
            existing, _rows = load_forward_auxiliary_set(
                str(final), date, bucket, prefix, seal, seal_sha)
            if existing != descriptor:
                raise cr.ReceiptError(
                    "AUX_SET_CONFLICT", "existing digest directory differs")
            shutil.rmtree(pending)
            pending = None
            return str(final), descriptor
        os.rename(pending, final)
        pending = None
        return str(final), descriptor
    finally:
        if pending and os.path.isdir(pending):
            shutil.rmtree(pending)


def _capture_gaps(receipt, date):
    """Validate the receipt's gap list independently of any CSV projection."""
    raw = receipt.get("gaps")
    if not isinstance(raw, list):
        raise cr.ReceiptError("AUX_SET_INVALID", "capture gaps is not a list")
    lo, hi = wc.day_start_us(date), wc.day_start_us(date) + DAY_US
    out = []
    previous_end = None
    for item in raw:
        if (not isinstance(item, dict)
                or set(item) != {"start_us", "end_us"}):
            raise cr.ReceiptError(
                "AUX_SET_INVALID", "capture gap entry is malformed")
        start, end = item.get("start_us"), item.get("end_us")
        if (not isinstance(start, int) or isinstance(start, bool)
                or not isinstance(end, int) or isinstance(end, bool)
                or not (lo <= start < end <= hi)):
            raise cr.ReceiptError(
                "AUX_SET_INVALID", "capture gap is outside the sealed date")
        if previous_end is not None and start < previous_end:
            raise cr.ReceiptError(
                "AUX_SET_INVALID", "capture gaps overlap or are unsorted")
        out.append({"start_us": start, "end_us": end})
        previous_end = end
    return out


def _validate_gap_projection(payload, date, capture_receipt):
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8")))
        rows = list(reader)
    except (UnicodeDecodeError, csv.Error) as exc:
        raise cr.ReceiptError("AUX_SET_INVALID", str(exc))
    if reader.fieldnames != ["start_us", "end_us"]:
        raise cr.ReceiptError("AUX_SET_INVALID", "capture gap columns invalid")
    got = []
    lo, hi = wc.day_start_us(date), wc.day_start_us(date) + DAY_US
    for row in rows:
        try:
            start, end = int(row["start_us"]), int(row["end_us"])
        except (KeyError, TypeError, ValueError):
            raise cr.ReceiptError("AUX_SET_INVALID", "capture gap row invalid")
        if not (lo <= start < end <= hi):
            raise cr.ReceiptError("AUX_SET_INVALID", "capture gap outside date")
        got.append({"start_us": start, "end_us": end})
    want = _capture_gaps(capture_receipt, date)
    if got != want:
        raise cr.ReceiptError(
            "AUX_SET_INVALID", "capture gap projection/receipt mismatch")


def _require_control_contract(row, *, family, source_kind, logical,
                              evidence_binding, version_resolution,
                              canonical_source):
    expected = {
        "storage_mode": "LOCAL_FROZEN_BACKFILL",
        "family": family,
        "source_kind": source_kind,
        "logical_source_key": logical,
        "table": None,
        "channel": None,
        "evidence_binding": evidence_binding,
        "version_resolution": version_resolution,
        "canonical_source": canonical_source,
    }
    mismatched = [name for name, value in expected.items()
                  if row.get(name) != value]
    if mismatched:
        raise cr.ReceiptError(
            "AUX_SET_INVALID", "%s contract mismatch: %s" %
            (family, ",".join(sorted(mismatched))))


def load_forward_auxiliary_set(path, date, bucket, prefix, seal, seal_sha):
    """Validate and pin an explicit forward auxiliary bundle."""
    reader = cr._BundleReader(path)
    try:
        descriptor = json.loads(reader.read(
            "AUX_SET.json", max_bytes=cr.MAX_AUX_DESCRIPTOR_BYTES)[0])
        if not isinstance(descriptor, dict):
            raise cr.ReceiptError("AUX_SET_INVALID", "root is not an object")
        if descriptor.get("schema_version") != FORWARD_AUX_SCHEMA:
            raise cr.ReceiptError("AUX_SET_INVALID", "wrong schema_version")
        if descriptor.get("mode") != "FORWARD_CANONICAL_CAPTURE":
            raise cr.ReceiptError("AUX_SET_INVALID", "wrong forward mode")
        if (descriptor.get("date") != date
                or descriptor.get("bucket") != bucket
                or descriptor.get("prefix") != prefix):
            raise cr.ReceiptError(
                "AUX_SET_INVALID", "date/bucket/prefix mismatch")
        if (descriptor.get("seal_sha256") != seal_sha
                or descriptor.get("manifest_date_sha256")
                != seal.get("manifest_date_sha256")):
            raise cr.ReceiptError("AUX_SET_INVALID", "seal binding mismatch")
        if descriptor.get("generation_witness_contract") != \
                _generation_witness_contract(
                    {"sha256": seal_sha, "sealed_at": seal.get("sealed_at")},
                    date, bucket, prefix):
            raise cr.ReceiptError(
                "AUX_SET_INVALID", "generation witness contract mismatch")
        projection = {key: value for key, value in descriptor.items()
                      if key != "aux_set_sha256"}
        digest = cr.canonical_sha256(projection)
        if (descriptor.get("aux_set_sha256") != digest
                or pathlib.Path(path).name != "aux-set=%s" % digest):
            raise cr.ReceiptError("AUX_SET_INVALID", "bundle digest mismatch")

        allowed_families = {
            "manifest_date_projection",
            "corrections_at_cutoff", "capture_gap_receipt",
            "capture_gaps_projection", "l2_quality",
        }
        objects = descriptor.get("objects")
        if not isinstance(objects, list) or not objects:
            raise cr.ReceiptError("AUX_SET_INVALID", "objects are missing")
        seen_keys, seen_logical, declared_files = set(), set(), set()
        enriched = []
        for raw in objects:
            if not isinstance(raw, dict):
                raise cr.ReceiptError("AUX_SET_INVALID", "object malformed")
            row = copy.deepcopy(raw)
            key = cr._safe_rel(row.get("key"), "auxiliary S3 key")
            logical = cr._safe_rel(
                row.get("logical_source_key"), "auxiliary logical key")
            if (row.get("bucket") != bucket
                    or not key.startswith(prefix + "/")):
                raise cr.ReceiptError(
                    "AUX_SET_INVALID", "object bucket/prefix mismatch")
            if key in seen_keys or logical in seen_logical:
                raise cr.ReceiptError("AUX_SET_INVALID", "duplicate object")
            seen_keys.add(key)
            seen_logical.add(logical)
            if (row.get("date") != date
                    or row.get("family") not in allowed_families):
                raise cr.ReceiptError(
                    "AUX_SET_INVALID", "object date/family mismatch")
            cr._check_expected(row.get("size"), row.get("sha256"), logical)
            if (row.get("seal_binding") != seal_sha
                    or row.get("required") is not True
                    or row.get("durability_scope") is not True
                    or row.get("research_candidate") is not True
                    or row.get("exposure_policy")
                    != "PENDING_ELIGIBILITY_TAG"):
                raise cr.ReceiptError(
                    "AUX_SET_INVALID", "object eligibility/binding invalid")
            storage_mode = row.get("storage_mode")
            local_rel = row.get("local_relpath")
            if (row.get("family") == "corrections_at_cutoff"
                    and row.get("source_kind") == "correction"
                    and storage_mode != FORWARD_LARGE_ATTESTATION):
                raise cr.ReceiptError(
                    "AUX_SET_INVALID",
                    "late_rows must use the large canonical attestation")
            if storage_mode == FORWARD_LARGE_ATTESTATION:
                expected_late_key = cr._join_key(
                    prefix, "warehouse", "corrections",
                    "date=%s" % date, "late_rows.ndjson")
                if (row.get("family") != "corrections_at_cutoff"
                        or row.get("source_kind") != "correction"
                        or row.get("logical_source_key") != cr._join_key(
                            "warehouse", "corrections", "date=%s" % date,
                            "late_rows.ndjson")
                        or key != expected_late_key
                        or local_rel is not None
                        or row.get("expected_version_id") is not None
                        or row.get("mutable_source") is not False
                        or row.get("version_resolution")
                        != "FORWARD_CURRENT_EXACT"
                        or row.get("canonical_source")
                        != "LOCAL_LARGE_ATTESTATION_THEN_EXACT_S3_VERSION"):
                    raise cr.ReceiptError(
                        "AUX_SET_INVALID",
                        "large correction attestation invalid")
            elif storage_mode == "LOCAL_FROZEN_BACKFILL":
                if row.get("expected_version_id") is not None:
                    raise cr.ReceiptError(
                        "AUX_SET_INVALID", "control preclaims VersionId")
                if row["size"] > cr.MAX_DATE_CONTROL_BYTES:
                    raise cr.ReceiptError(
                        "AUX_SET_INVALID",
                        "retained control exceeds byte limit")
                rel = cr._safe_rel(local_rel, "auxiliary local path")
                declared_files.add(rel)
                payload, _fingerprint = reader.read(
                    rel, max_bytes=cr.MAX_DATE_CONTROL_BYTES)
                if (len(payload) != row["size"]
                        or hashlib.sha256(payload).hexdigest()
                        != row["sha256"]):
                    raise cr.ReceiptError(
                        "AUX_SET_INVALID", "local control bytes mismatch")
                row["_local_path"] = str(pathlib.Path(path, *rel.split("/")))
                row["_local_payload"] = payload
                if key != expected_control_key(row, prefix, date):
                    raise cr.ReceiptError(
                        "AUX_SET_INVALID", "small control key is not derived")
            else:
                raise cr.ReceiptError("AUX_SET_INVALID", "unknown storage mode")
            enriched.append(row)

        desired_projection = [{
            "bucket": row["bucket"],
            "key": row["key"],
            "logical_source_key": row["logical_source_key"],
            "source_kind": row["source_kind"],
            "family": row["family"],
            "date": row["date"],
            "size": row["size"],
            "sha256": row["sha256"],
            "seal_binding": row["seal_binding"],
            "evidence_binding": row["evidence_binding"],
            "required": row["required"],
            "durability_scope": row["durability_scope"],
            "research_candidate": row["research_candidate"],
            "exposure_policy": row["exposure_policy"],
            "version_resolution": row["version_resolution"],
            "canonical_source": row["canonical_source"],
        } for row in objects]
        desired_projection.sort(key=lambda row: (
            row["logical_source_key"], row["bucket"], row["key"]))
        if (not cr.SHA256_RE.match(
                str(descriptor.get("desired_set_sha256") or ""))
                or cr.canonical_sha256(desired_projection)
                != descriptor["desired_set_sha256"]):
            raise cr.ReceiptError(
                "AUX_SET_INVALID", "desired set digest mismatch")

        actual_files = set()
        for base, dirs, files in os.walk(path):
            for name in dirs:
                if os.path.islink(os.path.join(base, name)):
                    raise cr.ReceiptError(
                        "AUX_SET_INVALID", "symlink directory")
            for name in files:
                full = os.path.join(base, name)
                if os.path.islink(full):
                    raise cr.ReceiptError("AUX_SET_INVALID", "symlink file")
                rel = os.path.relpath(full, path).replace(os.sep, "/")
                if rel != "AUX_SET.json":
                    actual_files.add(rel)
        if actual_files != declared_files:
            raise cr.ReceiptError(
                "AUX_SET_INVALID", "declared/local file set mismatch")
        by_family = {}
        for row in enriched:
            by_family.setdefault(row["family"], []).append(row)
        if len(by_family.get("manifest_date_projection", [])) != 1:
            raise cr.ReceiptError(
                "AUX_SET_INVALID", "manifest projection missing")
        manifest = by_family["manifest_date_projection"][0]
        expected_manifest_key = cr._join_key(
            prefix, "warehouse", "publication-snapshots", "v1",
            "date=%s" % date, "manifest",
            "sha256=%s" % manifest["sha256"], "manifest.csv")
        if (manifest.get("logical_source_key") != "warehouse/manifest.csv"
                or manifest.get("key") != expected_manifest_key
                or manifest.get("source_kind") != "warehouse_manifest_day"):
            raise cr.ReceiptError(
                "AUX_SET_INVALID", "manifest semantics invalid")
        _require_control_contract(
            manifest, family="manifest_date_projection",
            source_kind="warehouse_manifest_day",
            logical="warehouse/manifest.csv",
            evidence_binding={
                "manifest_date_sha256": seal.get("manifest_date_sha256")},
            version_resolution="WRITE_ONCE_CONTENT_ADDRESSED_EXACT",
            canonical_source="PLANNED_DATE_SCOPED_CONTROL_SYNC")
        day_digest, day_rows = cr._manifest_projection_bytes(
            manifest["_local_payload"], date)
        all_rows = list(csv.DictReader(io.StringIO(
            manifest["_local_payload"].decode("utf-8"), newline="")))
        if (day_digest != seal.get("manifest_date_sha256")
                or len(day_rows) != len(all_rows)):
            raise cr.ReceiptError(
                "AUX_SET_INVALID", "manifest is not date-exact")

        corr = by_family.get("corrections_at_cutoff", [])
        if len(corr) not in (0, 2):
            raise cr.ReceiptError("AUX_SET_INVALID", "corrections incomplete")
        if corr:
            late = next((row for row in corr
                         if row["source_kind"] == "correction"), None)
            ledger = next((row for row in corr
                           if row["source_kind"]
                           == "corrections_ledger_day"), None)
            if not late or not ledger:
                raise cr.ReceiptError(
                    "AUX_SET_INVALID", "corrections roles mismatch")
            projection_bytes, ledger_rows = cr._ledger_projection(
                ledger["_local_payload"], date)
            if projection_bytes != ledger["_local_payload"]:
                raise cr.ReceiptError(
                    "AUX_SET_INVALID", "ledger is not date-exact")
            _counts, validation = cr._correction_validation_from_ledger(
                ledger_rows, date)
            ledger_evidence = {
                "ledger_day_sha256": ledger["sha256"],
                "late_rows_validation": validation,
            }
            if late.get("evidence_binding") != ledger_evidence:
                raise cr.ReceiptError(
                    "AUX_SET_INVALID",
                    "large correction validation evidence mismatch")
            _require_control_contract(
                ledger, family="corrections_at_cutoff",
                source_kind="corrections_ledger_day",
                logical=cr._join_key(
                    "warehouse", "corrections", "date=%s" % date,
                    "ledger_day.ndjson"),
                evidence_binding=ledger_evidence,
                version_resolution="WRITE_ONCE_CONTENT_ADDRESSED_EXACT",
                canonical_source="PLANNED_DATE_SCOPED_CONTROL_SYNC")

        capture_rows = by_family.get("capture_gap_receipt", [])
        if len(capture_rows) != 1:
            raise cr.ReceiptError("AUX_SET_INVALID", "capture receipt missing")
        _require_control_contract(
            capture_rows[0], family="capture_gap_receipt",
            source_kind="capture_gap_receipt",
            logical=cr._join_key(
                "control", "quality", "v1", "date=%s" % date,
                "capture_gap_receipt.json"),
            evidence_binding="sealed firehose inventory",
            version_resolution="WRITE_ONCE_CONTENT_ADDRESSED_EXACT",
            canonical_source="PLANNED_SEALED_DAY_CONTROL_SYNC")
        try:
            capture = json.loads(capture_rows[0]["_local_payload"])
        except ValueError as exc:
            raise cr.ReceiptError("AUX_SET_INVALID", str(exc))
        valid, reason = cr._validate_capture_receipt(capture, seal, date)
        if not valid:
            raise cr.ReceiptError("AUX_SET_INVALID", reason)
        capture_gaps = _capture_gaps(capture, date)
        gap_rows = by_family.get("capture_gaps_projection", [])
        if len(gap_rows) > 1:
            raise cr.ReceiptError(
                "AUX_SET_INVALID", "duplicate capture gap projection")
        if gap_rows:
            _require_control_contract(
                gap_rows[0], family="capture_gaps_projection",
                source_kind="capture_gaps_projection",
                logical=cr._join_key(
                    "control", "quality", "v1", "date=%s" % date,
                    "capture_gaps.csv"),
                evidence_binding="capture receipt gap intervals",
                version_resolution="WRITE_ONCE_CONTENT_ADDRESSED_EXACT",
                canonical_source="PLANNED_SEALED_DAY_CONTROL_SYNC")
            _validate_gap_projection(
                gap_rows[0]["_local_payload"], date, capture)
        elif capture_gaps:
            raise cr.ReceiptError(
                "AUX_SET_INVALID", "capture gap projection is missing")

        l2_required = (
            any(row.get("table") == "orderbooks_full"
                for row in seal.get("archive_file_stats", []))
            or any(
                row.get("file", "").startswith("date=%s/" % date)
                and os.path.basename(row["file"]).startswith("l2_")
                for row in seal.get("raw_files", [])))
        l2_rows = by_family.get("l2_quality", [])
        if len(l2_rows) != int(l2_required):
            raise cr.ReceiptError(
                "AUX_SET_INVALID", "L2 receipt count mismatch")
        if l2_rows:
            _require_control_contract(
                l2_rows[0], family="l2_quality",
                source_kind="l2_quality_receipt",
                logical=cr._join_key(
                    "control", "quality", "v1", "date=%s" % date,
                    "l2_gaps.json"),
                evidence_binding="sealed l2 inventory",
                version_resolution="WRITE_ONCE_CONTENT_ADDRESSED_EXACT",
                canonical_source="PLANNED_SEALED_DAY_CONTROL_SYNC")
            try:
                quality, reason = research_release.validate_l2_receipt(
                    json.loads(l2_rows[0]["_local_payload"]), seal, date)
            except Exception as exc:
                raise cr.ReceiptError("AUX_SET_INVALID", str(exc))
            if quality is None:
                raise cr.ReceiptError("AUX_SET_INVALID", reason)
        reader.assert_stable()
        return descriptor, enriched
    except (UnicodeDecodeError, ValueError) as exc:
        raise cr.ReceiptError("AUX_SET_INVALID", str(exc))
    finally:
        reader.close()


def forward_version_resolution_targets(auxiliary):
    rows = [row for row in auxiliary
            if (row.get("storage_mode") == FORWARD_LARGE_ATTESTATION
                and row.get("source_kind") == "correction")]
    rows.sort(key=lambda row: row["logical_source_key"])
    return rows


def _normalize_witness_object(descriptor, date, bucket, prefix,
                              generation_witness, witness_object):
    if not isinstance(witness_object, dict):
        raise cr.ReceiptError(
            "FORWARD_VERSION_BINDING_INVALID", "witness object missing")
    exact = _exact_s3_binding(witness_object, "generation witness")
    raw = generation_witness_bytes(generation_witness)
    digest = hashlib.sha256(raw).hexdigest()
    expected_key = expected_generation_witness_key(prefix, date, digest)
    contract = descriptor["generation_witness_contract"]
    if (exact["bucket"] != bucket or exact["key"] != expected_key
            or exact["size"] != len(raw) or exact["sha256"] != digest
            or not exact["key"].startswith(contract["key_prefix"])):
        raise cr.ReceiptError(
            "FORWARD_VERSION_BINDING_INVALID",
            "generation witness exact object mismatch")
    validated = validate_generation_witness(
        generation_witness, date, bucket, prefix,
        descriptor["seal_sha256"], generation_witness["seal"]["size"],
        contract["seal_sealed_at_utc"])
    if validated["generation_authority"] not in \
            contract["allowed_generation_authorities"]:
        raise cr.ReceiptError(
            "FORWARD_VERSION_BINDING_INVALID",
            "generation authority is forbidden for this date")
    return exact, validated


def _normalize_version_resolutions(descriptor, auxiliary, date, bucket,
                                   prefix, generation_witness,
                                   witness_object, late_resolutions):
    exact_witness, witness = _normalize_witness_object(
        descriptor, date, bucket, prefix, generation_witness, witness_object)
    late_rows = forward_version_resolution_targets(auxiliary)
    late_by_logical = {row["logical_source_key"]: row for row in late_rows}
    if not isinstance(late_resolutions, list):
        raise cr.ReceiptError(
            "FORWARD_VERSION_BINDING_INVALID",
            "late resolution set is not a list")
    objects, evidence_rows, seen = [], [], set()
    catalog_generation_id = witness["catalog_generation"]["generation_id"]
    dim_generation_id = witness["dim_generation"]["generation_id"]
    witness_sha = exact_witness["sha256"]
    for raw in witness["objects"]:
        logical = raw["logical_source_key"]
        evidence = {
            "logical_source_key": logical,
            "key": raw["key"],
            "selected_version_id": raw["VersionId"],
            "generation_witness_sha256": witness_sha,
            "generation_witness_version_id": exact_witness["VersionId"],
            "generation_authority": witness["generation_authority"],
            "catalog_dim_coherence_claim":
                witness["catalog_dim_coherence_claim"],
            "catalog_generation_id": catalog_generation_id,
            "dim_generation_id": dim_generation_id,
            "selection_rule": "EXACT_MEMBER_FROM_GENERATION_WITNESS",
        }
        evidence_digest = cr.canonical_sha256(evidence)
        evidence_rows.append(evidence)
        objects.append({
            "logical_source_key": logical, "bucket": raw["bucket"],
            "key": raw["key"], "size": raw["size"],
            "sha256": raw["sha256"], "VersionId": raw["VersionId"],
            "LastModified": raw["LastModified"],
            "resolution": FORWARD_WITNESS_MEMBER_RESOLUTION,
            "resolver_evidence_sha256": evidence_digest,
            "source_kind": raw["source_kind"], "family": raw["family"],
            "table": raw["table"], "channel": raw["channel"],
            "required": True,
        })
        seen.add(logical)
    for raw in late_resolutions:
        if not isinstance(raw, dict) or set(raw) != {
                "logical_source_key", "bucket", "key", "size", "sha256",
                "VersionId", "LastModified", "resolution",
                "resolver_evidence"}:
            raise cr.ReceiptError(
                "FORWARD_VERSION_BINDING_INVALID", "resolution shape")
        logical = raw["logical_source_key"]
        if logical in seen or logical not in late_by_logical:
            raise cr.ReceiptError(
                "FORWARD_VERSION_BINDING_INVALID", "resolution target invalid")
        seen.add(logical)
        late = late_by_logical[logical]
        expected = late
        if raw["bucket"] != bucket or raw["key"] != expected["key"]:
            raise cr.ReceiptError(
                "FORWARD_VERSION_BINDING_INVALID", "resolution key mismatch")
        cr._check_expected(raw["size"], raw["sha256"], logical)
        if (raw["size"] != late["size"]
                or raw["sha256"] != late["sha256"]):
            raise cr.ReceiptError(
                "FORWARD_VERSION_BINDING_INVALID", "late hash mismatch")
        version_id = raw["VersionId"]
        if (not isinstance(version_id, str) or not version_id.strip()
                or version_id.lower() == "null"):
            raise cr.ReceiptError(
                "FORWARD_VERSION_BINDING_INVALID", "VersionId invalid")
        modified = cr._canonical_utc(
            raw["LastModified"], "resolved S3 LastModified")
        if raw["resolution"] not in {
                "CURRENT_EXACT_SHA256_MATCH",
                "HISTORICAL_EXACT_SHA256_MATCH"}:
            raise cr.ReceiptError(
                "FORWARD_VERSION_BINDING_INVALID", "late resolution invalid")
        metadata = {
            "source_kind": late["source_kind"], "family": late["family"],
            "table": late.get("table"), "channel": late.get("channel"),
        }
        evidence = copy.deepcopy(raw["resolver_evidence"])
        if (not isinstance(evidence, dict)
                or evidence.get("logical_source_key") != logical
                or evidence.get("key") != raw["key"]
                or evidence.get("selected_version_id") != version_id):
            raise cr.ReceiptError(
                "FORWARD_VERSION_BINDING_INVALID", "resolver evidence invalid")
        if evidence.get("selection_rule") != FORWARD_VERSION_SELECTION_RULE:
            raise cr.ReceiptError(
                "FORWARD_VERSION_BINDING_INVALID",
                "late resolver selection rule invalid")
        evidence_digest = cr.canonical_sha256(evidence)
        evidence_rows.append(evidence)
        objects.append({
            "logical_source_key": logical, "bucket": bucket,
            "key": raw["key"], "size": raw["size"],
            "sha256": raw["sha256"], "VersionId": version_id,
            "LastModified": modified, "resolution": raw["resolution"],
            "resolver_evidence_sha256": evidence_digest,
            "source_kind": metadata["source_kind"],
            "family": metadata["family"], "table": metadata.get("table"),
            "channel": metadata.get("channel"), "required": True,
        })
    if set(late_by_logical) != {
            row["logical_source_key"] for row in late_resolutions}:
        raise cr.ReceiptError(
            "FORWARD_VERSION_BINDING_INVALID", "late resolution set incomplete")
    objects.sort(key=lambda row: row["logical_source_key"])
    evidence_rows.sort(key=lambda row: row["logical_source_key"])
    return exact_witness, witness, objects, evidence_rows


def write_forward_version_binding(output_root, descriptor, auxiliary,
                                  date, bucket, prefix, generation_witness,
                                  witness_object, late_resolutions):
    exact_witness, _witness, objects, evidence = _normalize_version_resolutions(
        descriptor, auxiliary, date, bucket, prefix, generation_witness,
        witness_object, late_resolutions)
    projection = {
        "schema_version": FORWARD_VERSION_BINDING_SCHEMA,
        "state": FORWARD_VERSION_BINDING_STATE,
        "date": date, "bucket": bucket, "prefix": prefix,
        "seal_sha256": descriptor["seal_sha256"],
        "aux_set_sha256": descriptor["aux_set_sha256"],
        "selection_rule":
            "GENERATION_WITNESS_PLUS_FROZEN_LARGE_HASH_EXACT_VERSION",
        "generation_witness": copy.deepcopy(generation_witness),
        "generation_witness_object": exact_witness,
        "objects": objects, "object_set_sha256": cr.canonical_sha256(objects),
        "resolver_evidence": evidence,
        "resolver_evidence_set_sha256": cr.canonical_sha256(evidence),
    }
    binding_sha = cr.canonical_sha256(projection)
    payload = dict(projection, version_binding_sha256=binding_sha)
    root = pathlib.Path(output_root) / ("date=%s" % date)
    root.mkdir(parents=True, exist_ok=True)
    final = root / ("VERSION-BINDING-%s.json" % binding_sha)
    if final.exists():
        existing = json.loads(cr._freeze_file(
            str(final), max_bytes=MAX_VERSION_BINDING_BYTES)[0])
        if existing != payload:
            raise cr.ReceiptError(
                "FORWARD_VERSION_BINDING_CONFLICT", str(final))
    else:
        cr.write_atomic_json(final, payload)
    return str(final), payload


def load_forward_version_binding(path, descriptor, auxiliary,
                                 date, bucket, prefix):
    try:
        payload = json.loads(cr._freeze_file(
            path, max_bytes=MAX_VERSION_BINDING_BYTES)[0])
    except (UnicodeDecodeError, ValueError) as exc:
        raise cr.ReceiptError("FORWARD_VERSION_BINDING_INVALID", str(exc))
    if not isinstance(payload, dict) or set(payload) != {
            "schema_version", "state", "date", "bucket", "prefix",
            "seal_sha256", "aux_set_sha256", "selection_rule",
            "generation_witness", "generation_witness_object",
            "objects", "object_set_sha256", "resolver_evidence",
            "resolver_evidence_set_sha256", "version_binding_sha256"}:
        raise cr.ReceiptError(
            "FORWARD_VERSION_BINDING_INVALID", "binding shape")
    if (payload["schema_version"] != FORWARD_VERSION_BINDING_SCHEMA
            or payload["state"] != FORWARD_VERSION_BINDING_STATE
            or payload["date"] != date or payload["bucket"] != bucket
            or payload["prefix"] != prefix
            or payload["seal_sha256"] != descriptor["seal_sha256"]
            or payload["aux_set_sha256"] != descriptor["aux_set_sha256"]
            or payload["selection_rule"]
            != "GENERATION_WITNESS_PLUS_FROZEN_LARGE_HASH_EXACT_VERSION"):
        raise cr.ReceiptError(
            "FORWARD_VERSION_BINDING_INVALID", "binding authority mismatch")
    projection = {name: value for name, value in payload.items()
                  if name != "version_binding_sha256"}
    digest = cr.canonical_sha256(projection)
    if (payload["version_binding_sha256"] != digest
            or pathlib.Path(path).name
            != "VERSION-BINDING-%s.json" % digest
            or payload["object_set_sha256"]
            != cr.canonical_sha256(payload["objects"])
            or payload["resolver_evidence_set_sha256"]
            != cr.canonical_sha256(payload["resolver_evidence"])):
        raise cr.ReceiptError(
            "FORWARD_VERSION_BINDING_INVALID", "binding digest mismatch")
    evidence_by_logical = {
        row.get("logical_source_key"): row
        for row in payload["resolver_evidence"] if isinstance(row, dict)}
    if len(evidence_by_logical) != len(payload["resolver_evidence"]):
        raise cr.ReceiptError(
            "FORWARD_VERSION_BINDING_INVALID", "resolver evidence duplicates")
    late_resolutions = []
    for row in payload["objects"]:
        if not isinstance(row, dict):
            raise cr.ReceiptError(
                "FORWARD_VERSION_BINDING_INVALID", "bound object malformed")
        evidence = evidence_by_logical.get(row.get("logical_source_key"))
        if (not isinstance(evidence, dict)
                or cr.canonical_sha256(evidence)
                != row.get("resolver_evidence_sha256")):
            raise cr.ReceiptError(
                "FORWARD_VERSION_BINDING_INVALID", "evidence digest mismatch")
        if row.get("resolution") == FORWARD_WITNESS_MEMBER_RESOLUTION:
            continue
        late_resolutions.append({
            name: row[name] for name in (
                "logical_source_key", "bucket", "key", "size", "sha256",
                "VersionId", "LastModified", "resolution")
        })
        late_resolutions[-1]["resolver_evidence"] = evidence
    normalized_witness, _witness, normalized, normalized_evidence = \
        _normalize_version_resolutions(
            descriptor, auxiliary, date, bucket, prefix,
            payload["generation_witness"],
            payload["generation_witness_object"], late_resolutions)
    if (normalized != payload["objects"]
            or normalized_evidence != payload["resolver_evidence"]
            or normalized_witness != payload["generation_witness_object"]):
        raise cr.ReceiptError(
            "FORWARD_VERSION_BINDING_INVALID", "binding normalization mismatch")
    return payload, {
        row["logical_source_key"]: row for row in normalized}


def build_forward_inventory(date, bucket, prefix, raw_root, warehouse_root,
                            quality_dir, aux_bundle, version_binding,
                            include_rfq_durability=False,
                            fresh_rfq_eligibility=None):
    """Build the forward desired set without making an S3 call.

    RFQ is excluded by default under the old-lineage no-repair ruling.  A
    future day may admit only the exact analysis objects in a revalidated
    ``fresh-rfq-daily-eligibility-v1`` package.  The old function-only boolean
    remains solely for compatibility with offline migration tests; production
    callers use the content-addressed gate path, never an unbound boolean.
    """
    if not bucket or "/" in bucket:
        raise cr.ReceiptError("INVALID_BUCKET", repr(bucket))
    prefix = cr._safe_rel(prefix.strip("/"), "canonical prefix")
    (seal, binding, _manifest_payload, manifest_digest, _manifest_rows,
     raw_proofs, fact_proofs) = _forward_authoritative_day_inputs(
         date, raw_root, warehouse_root, quality_dir)
    if include_rfq_durability and fresh_rfq_eligibility is not None:
        raise cr.ReceiptError(
            "RFQ_ELIGIBILITY_INVALID",
            "legacy RFQ capability and daily eligibility are mutually exclusive")
    eligibility = None
    eligible_by_key = {}
    if fresh_rfq_eligibility is not None:
        try:
            eligibility = fresh_rfq_gate.load_package(
                pathlib.Path(fresh_rfq_eligibility), expected_date=date)
        except (fresh_rfq_gate.EligibilityError, OSError) as exc:
            raise cr.ReceiptError(
                "RFQ_ELIGIBILITY_INVALID", str(exc)) from exc
        if (eligibility.get("research_ready") is not True
                or eligibility.get("old_lineage_state")
                != "DATA_INTEGRITY_BLOCKED"
                or eligibility.get("repair_state") != "FORBIDDEN"
                or eligibility.get("old_lineage_overlap_count") != 0):
            raise cr.ReceiptError(
                "RFQ_ELIGIBILITY_INVALID", "daily marker is not fail-closed")
        for row in eligibility["eligible_objects"]:
            key = row["key"]
            if (row["bucket"] != bucket
                    or not key.startswith(prefix + "/raw/")
                    or key in eligible_by_key):
                raise cr.ReceiptError(
                    "RFQ_ELIGIBILITY_INVALID", key)
            eligible_by_key[key] = row
    seal_sha = binding["sha256"]
    objects = []
    seen = {"physical": set(), "logical": set()}
    families = []

    included_raw_proofs = []
    deferred_rfq_proofs = []
    observed_eligible = set()
    for proof in sorted(raw_proofs, key=lambda item: item["file"]):
        rel = proof["file"]
        path_date, basename = cr._raw_rel_parts(rel)
        kind, channel = cr._raw_classification(rel)
        is_rfq = kind in {"raw_rfq", "raw_rfq_receipts"}
        eligible_row = None
        if is_rfq and eligibility is not None:
            canonical_key = cr._join_key(prefix, "raw", rel)
            eligible_row = eligible_by_key.get(canonical_key)
            if eligible_row is None:
                deferred_rfq_proofs.append(proof)
                continue
            if (kind != "raw_rfq"
                    or proof["size"] != eligible_row["size"]
                    or proof["sha256"] != eligible_row["sha256"]):
                raise cr.ReceiptError(
                    "RFQ_ELIGIBILITY_INVALID",
                    "%s does not match the sealed raw proof" % canonical_key)
            observed_eligible.add(canonical_key)
        elif is_rfq and not include_rfq_durability:
            deferred_rfq_proofs.append(proof)
            continue
        included_raw_proofs.append(proof)
        obj = cr._entry(
            bucket, cr._join_key(prefix, "raw", rel), cr._join_key("raw", rel),
            kind, path_date, proof["size"], proof["sha256"], seal_sha,
            channel=channel, family="raw_durability",
            evidence_binding="seal.raw_files",
            attestation_class="DAY_SEAL_ATTESTED",
            required=not is_rfq,
            durability_scope=True, research_candidate=False,
            exposure_policy=("FORBIDDEN_RFQ_DEFAULT" if is_rfq
                             else "FORBIDDEN_RAW"),
            version_resolution=(
                "FRESH_RFQ_ELIGIBILITY_EXACT_VERSION" if eligible_row
                else "SEALED_CURRENT_EXACT"),
            canonical_source=(
                "FRESH_RFQ_DAILY_ELIGIBILITY" if eligible_row
                else "EXISTING_CANONICAL_SYNC"),
            expected_version_id=(
                eligible_row["version_id"] if eligible_row else None))
        cr._add_unique(objects, seen, obj)
    if eligibility is not None and observed_eligible != set(eligible_by_key):
        missing = sorted(set(eligible_by_key) - observed_eligible)
        raise cr.ReceiptError(
            "RFQ_ELIGIBILITY_INVALID",
            "eligible object is absent from seal.raw_files: %s" % missing[:1])
    families.append(cr._family(
        "raw_durability", "REQUIRED_CORE", "seal.raw_files",
        len(included_raw_proofs), len(included_raw_proofs),
        "PRESENT_VERIFIED",
        semantic_sha256=cr.canonical_sha256(included_raw_proofs)))
    if deferred_rfq_proofs:
        families.append(cr._family(
            "raw_rfq_deferred", "CONDITIONAL",
            ("old/receipt/watermark RFQ remains excluded"
             if eligibility is not None else
             "operator RFQ branch closed; separate future receipt required"),
            0, 0, "NOT_APPLICABLE",
            ("NON_ELIGIBLE_RFQ_EXCLUDED" if eligibility is not None
             else "RFQ_BRANCH_CLOSED_NO_REPAIR"),
            semantic_sha256=cr.canonical_sha256(deferred_rfq_proofs)))

    for proof in sorted(fact_proofs, key=lambda item: item["file"]):
        rel, table = proof["file"], proof.get("table")
        obj = cr._entry(
            bucket, cr._join_key(prefix, "warehouse", "facts", rel),
            cr._join_key("warehouse", "facts", rel), "facts", date,
            proof["size"], proof["sha256"], seal_sha, table=table,
            channel=table, family="facts",
            evidence_binding="seal.archive_file_stats",
            attestation_class="DAY_SEAL_ATTESTED",
            durability_scope=True, research_candidate=True,
            exposure_policy="PENDING_ELIGIBILITY_TAG",
            version_resolution="SEALED_CURRENT_EXACT")
        cr._add_unique(objects, seen, obj)
    families.append(cr._family(
        "facts", "REQUIRED_CORE", "seal.archive_file_stats",
        len(fact_proofs), len(fact_proofs), "PRESENT_VERIFIED",
        semantic_sha256=cr.canonical_sha256(fact_proofs)))

    seal_rel = "seals/date=%s.json" % date
    cr._add_bytes_object(
        objects, seen, bucket=bucket,
        key=cr._join_key(prefix, "warehouse", seal_rel),
        logical=cr._join_key("warehouse", seal_rel), kind="seal", date=date,
        payload=binding["_payload"], seal_sha=seal_sha, family="seal",
        evidence_binding="self", attestation_class="DAY_SEAL_ATTESTED",
        durability_scope=True, research_candidate=True,
        exposure_policy="PENDING_ELIGIBILITY_TAG",
        version_resolution="SEALED_CURRENT_EXACT")
    families.append(cr._family(
        "seal", "REQUIRED_CORE", "frozen full_v2 seal", 1, 1,
        "PRESENT_VERIFIED", semantic_sha256=seal_sha))
    binding.update({
        "bucket": bucket,
        "key": cr._join_key(prefix, "warehouse", seal_rel),
        "VersionId": None,
        "manifest_date_sha256": manifest_digest,
    })

    # Catalog and dated dim come exclusively from the exact generation witness.
    # Mutable producer paths are never read and there is no timestamp fallback.
    descriptor, auxiliary = load_forward_auxiliary_set(
        aux_bundle, date, bucket, prefix, seal, seal_sha)
    version_descriptor, versions = load_forward_version_binding(
        version_binding, descriptor, auxiliary, date, bucket, prefix)
    binding["forward_version_binding_sha256"] = \
        version_descriptor["version_binding_sha256"]

    witness_payload = version_descriptor["generation_witness"]
    witness = validate_generation_witness(
        witness_payload, date, bucket, prefix, seal_sha, binding["size"],
        descriptor["generation_witness_contract"]["seal_sealed_at_utc"])
    witness_seal = witness["seal"]
    if eligibility is not None:
        eligible_seal = eligibility["source_seal"]
        expected_eligible_seal = {
            "bucket": witness_seal["bucket"],
            "key": witness_seal["key"],
            "version_id": witness_seal["VersionId"],
            "size": witness_seal["size"],
            "sha256": witness_seal["sha256"],
        }
        if any(eligible_seal.get(field) != value
               for field, value in expected_eligible_seal.items()):
            raise cr.ReceiptError(
                "RFQ_ELIGIBILITY_INVALID",
                "daily RFQ gate is bound to a different exact day seal")
    seal_objects = [row for row in objects if row["source_kind"] == "seal"]
    if (len(seal_objects) != 1
            or witness_seal["key"] != seal_objects[0]["key"]
            or witness_seal["size"] != seal_objects[0]["size"]
            or witness_seal["sha256"] != seal_objects[0]["sha256"]):
        raise cr.ReceiptError(
            "FORWARD_VERSION_BINDING_INVALID", "witness seal mismatch")
    seal_objects[0]["_expected_version_id"] = witness_seal["VersionId"]
    seal_objects[0]["_expected_last_modified_utc"] = \
        witness_seal["LastModified"]

    witness_objects = [row for row in versions.values()
                       if row["family"] in {
                           "catalog_at_publication", "dim_snapshot"}]
    catalog_bound = sorted(
        (row for row in witness_objects
         if row["family"] == "catalog_at_publication"),
        key=lambda row: row["logical_source_key"])
    dim_bound = sorted(
        (row for row in witness_objects if row["family"] == "dim_snapshot"),
        key=lambda row: row["logical_source_key"])
    catalog_generation = witness["catalog_generation"]
    dim_generation = witness["dim_generation"]
    catalog_files = catalog_generation["files"]
    dim_files = dim_generation["files"]
    catalog_set_sha = cr.canonical_sha256(catalog_files)
    dim_set_sha = cr.canonical_sha256(dim_files)
    provenance = (FORWARD_CATALOG_PROVENANCE
                  if witness["generation_authority"]
                  == GENERATION_AUTHORITY_PRODUCER
                  else FORWARD_LEGACY_PROVENANCE)

    for row in catalog_bound + dim_bound:
        if row["family"] == "catalog_at_publication":
            source_evidence = {
                "provenance": provenance,
                "catalog_set_sha256": catalog_set_sha,
                "catalog_generation_id": catalog_generation["generation_id"],
                "catalog_dim_coherence_claim":
                    witness["catalog_dim_coherence_claim"],
                "generation_witness_sha256":
                    version_descriptor["generation_witness_object"]["sha256"],
            }
        else:
            source_evidence = {
                "provenance": provenance,
                "dim_set_sha256": dim_set_sha,
                "dim_generation_id": dim_generation["generation_id"],
                "catalog_dim_coherence_claim":
                    witness["catalog_dim_coherence_claim"],
            }
            if witness["catalog_dim_coherence_claim"]:
                source_evidence["catalog_generation_id"] = \
                    catalog_generation["generation_id"]
            else:
                source_evidence.update({
                    "dim_source_catalog_generation_id":
                        dim_generation["source_catalog_generation_id"],
                    "selected_catalog_generation_id":
                        catalog_generation["generation_id"],
                    "relationship":
                        "LEGACY_MIGRATION_COMBINATION_NOT_COHERENT",
                })
        evidence_binding = {
            "source_evidence": source_evidence,
            "forward_version_binding_sha256":
                version_descriptor["version_binding_sha256"],
            "resolver_evidence_sha256": row["resolver_evidence_sha256"],
            "resolution": row["resolution"],
        }
        obj = cr._entry(
            row["bucket"], row["key"], row["logical_source_key"],
            row["source_kind"], date, row["size"], row["sha256"], seal_sha,
            table=row.get("table"), channel=row.get("channel"),
            evidence_binding=evidence_binding, mutable_source=False,
            required=True, family=row["family"],
            attestation_class="PUBLICATION_CUTOFF_FROZEN",
            durability_scope=True, research_candidate=True,
            exposure_policy="PENDING_ELIGIBILITY_TAG",
            version_resolution=row["resolution"],
            canonical_source="EXACT_GENERATION_WITNESS_MEMBER",
            expected_version_id=row["VersionId"],
            expected_last_modified_utc=row["LastModified"])
        cr._add_unique(objects, seen, obj)

    exact_witness = version_descriptor["generation_witness_object"]
    witness_raw = generation_witness_bytes(witness_payload)
    witness_logical = exact_witness["key"][len(prefix) + 1:]
    witness_evidence = {
        "generation_authority": witness["generation_authority"],
        "catalog_dim_coherence_claim":
            witness["catalog_dim_coherence_claim"],
        "catalog_generation_id": catalog_generation["generation_id"],
        "dim_generation_id": dim_generation["generation_id"],
        "generation_witness_sha256": exact_witness["sha256"],
    }
    witness_obj = cr._entry(
        bucket, exact_witness["key"], witness_logical,
        "generation_witness", date, exact_witness["size"],
        exact_witness["sha256"], seal_sha,
        local_payload=witness_raw, evidence_binding=witness_evidence,
        mutable_source=False, required=True, family="generation_witness",
        attestation_class="PUBLICATION_CUTOFF_FROZEN",
        durability_scope=True, research_candidate=False,
        exposure_policy="NOT_RESEARCH_EXPOSED",
        version_resolution="EXACT_CONTENT_ADDRESSED_WITNESS_VERSION",
        canonical_source="EXISTING_CONTENT_ADDRESSED_GENERATION_WITNESS",
        expected_version_id=exact_witness["VersionId"],
        expected_last_modified_utc=exact_witness["LastModified"])
    cr._add_unique(objects, seen, witness_obj)

    aux_by_family = {}
    for row in auxiliary:
        aux_by_family.setdefault(row["family"], []).append(row)
        resolved = versions.get(row["logical_source_key"])
        evidence_binding = row.get("evidence_binding")
        if resolved is not None:
            evidence_binding = {
                "source_evidence": evidence_binding,
                "forward_version_binding_sha256":
                    version_descriptor["version_binding_sha256"],
                "resolver_evidence_sha256":
                    resolved["resolver_evidence_sha256"],
                "resolution": resolved["resolution"],
            }
        obj = cr._entry(
            row["bucket"], row["key"], row["logical_source_key"],
            row["source_kind"], row["date"], row["size"], row["sha256"],
            seal_sha, table=row.get("table"), channel=row.get("channel"),
            local_path=row.get("_local_path"),
            local_payload=row.get("_local_payload"),
            evidence_binding=evidence_binding,
            mutable_source=False,
            required=True, family=row["family"],
            attestation_class="PUBLICATION_CUTOFF_FROZEN",
            durability_scope=True,
            research_candidate=row["research_candidate"],
            exposure_policy=row["exposure_policy"],
            version_resolution=row["version_resolution"],
            canonical_source=row["canonical_source"],
            expected_version_id=(resolved["VersionId"]
                                 if resolved is not None else None),
            expected_last_modified_utc=(resolved["LastModified"]
                                        if resolved is not None else None))
        cr._add_unique(objects, seen, obj)

    families.append(cr._family(
        "dim_snapshot", "REQUIRED_RESEARCH",
        "exact generation witness",
        len(cr.DIM_REQUIRED), len(dim_bound), "PRESENT_VERIFIED",
        semantic_sha256=cr.canonical_sha256({
            "generation": dim_generation,
            "generation_authority": witness["generation_authority"],
            "version_binding_sha256":
                version_descriptor["version_binding_sha256"],
        })))

    families.append(cr._family(
        "generation_witness", "REQUIRED_CORE",
        "content-addressed exact producer/migration witness", 1, 1,
        "PRESENT_VERIFIED", semantic_sha256=exact_witness["sha256"]))

    families.append(cr._family(
        "manifest_date_projection", "REQUIRED_CORE",
        "date-only seal-bound manifest projection", 1,
        len(aux_by_family.get("manifest_date_projection", [])),
        "PRESENT_VERIFIED", semantic_sha256=manifest_digest))
    families.append(cr._family(
        "catalog_at_publication", "REQUIRED_RESEARCH",
        "exact generation witness",
        len(catalog_bound), len(catalog_bound), "PRESENT_VERIFIED",
        semantic_sha256=cr.canonical_sha256({
            "generation": catalog_generation,
            "generation_authority": witness["generation_authority"],
            "version_binding_sha256":
                version_descriptor["version_binding_sha256"],
        })))
    corr = aux_by_family.get("corrections_at_cutoff", [])
    families.append(cr._family(
        "corrections_at_cutoff", "CONDITIONAL",
        "frozen late_rows plus date-only ledger", len(corr), len(corr),
        "PRESENT_VERIFIED" if corr else "NOT_APPLICABLE",
        semantic_sha256=cr.canonical_sha256([{
            "key": row["key"], "size": row["size"],
            "sha256": row["sha256"],
        } for row in corr])))
    families.append(cr._family(
        "capture_gap_receipt", "REQUIRED_RESEARCH",
        "sealed firehose inventory", 1,
        len(aux_by_family.get("capture_gap_receipt", [])),
        "PRESENT_VERIFIED"))
    gap_count = len(aux_by_family.get("capture_gaps_projection", []))
    families.append(cr._family(
        "capture_gaps_projection", "CONDITIONAL",
        "date-only capture gap intervals", gap_count, gap_count,
        "PRESENT_VERIFIED" if gap_count else "NOT_APPLICABLE"))
    l2_required = (
        any(proof.get("table") == "orderbooks_full"
            for proof in fact_proofs)
        or any(
            proof["file"].startswith("date=%s/" % date)
            and os.path.basename(proof["file"]).startswith("l2_")
            for proof in raw_proofs))
    l2_count = len(aux_by_family.get("l2_quality", []))
    families.append(cr._family(
        "l2_quality", "CONDITIONAL", "required when L2 facts exist",
        int(l2_required), l2_count,
        "PRESENT_VERIFIED" if l2_count else "NOT_APPLICABLE"))
    objects.sort(key=lambda obj: (obj["logical_source_key"], obj["key"]))
    binding["families"] = cr._finalize_families(families, objects)
    return binding, objects


def _common_args(parser):
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
    historical = sub.add_parser("verify-historical-authority")
    _common_args(historical)
    freeze = sub.add_parser("freeze-forward")
    _common_args(freeze)
    freeze.add_argument(
        "--aux-root", default=os.path.join(
            wc.ROOT, "work", "live", "canonical_receipts", "forward-aux"))
    plan = sub.add_parser("plan-forward")
    _common_args(plan)
    plan.add_argument("--aux-bundle", required=True)
    plan.add_argument("--version-binding", required=True)
    plan.add_argument(
        "--fresh-rfq-eligibility",
        help="validated date=D/ELIGIBLE.json; admits only its exact RFQ set")
    shadow = sub.add_parser("shadow-forward")
    _common_args(shadow)
    shadow.add_argument("--aux-bundle", required=True)
    shadow.add_argument("--version-binding", required=True)
    shadow.add_argument(
        "--fresh-rfq-eligibility",
        help="validated date=D/ELIGIBLE.json; admits only its exact RFQ set")
    shadow.add_argument("--aws-cli", default="aws")
    shadow.add_argument("--metadata-only", action="store_true")
    shadow.add_argument("--probe-limit", type=int)
    shadow.add_argument(
        "--workers", type=int,
        choices=range(1, cr.MAX_VERIFY_WORKERS + 1), default=1,
        help="bounded verification workers (1-4; default: 1)")
    shadow.add_argument(
        "--output-root", default=os.path.join(
            wc.ROOT, "work", "live", "canonical_receipts", "forward-shadow"))
    args = parser.parse_args(argv)
    cfg = wc.load_config()
    raw_root = os.path.abspath(args.raw_root or cfg["raw_root"])
    warehouse_root = os.path.abspath(
        args.warehouse_root or cfg["warehouse_root"])
    quality_dir = os.path.abspath(args.quality_dir)
    if args.command == "verify-historical-authority":
        try:
            _seal, binding, *_rest = historical_authoritative_day_inputs(
                args.date, raw_root, warehouse_root, quality_dir)
        except cr.ReceiptError as exc:
            print("BLOCKED_INTEGRITY %s" % exc, file=os.sys.stderr)
            return 2
        authority = binding["historical_metadata_authority"]
        print(json.dumps({
            "schema_version": HISTORICAL_METADATA_AUTHORITY_SCHEMA,
            "state": "HISTORICAL_METADATA_AUTHORITY_VERIFIED",
            "date": args.date,
            "authority_sha256": cr.canonical_sha256(authority),
            "seal_sha256": binding["sha256"],
            "local_raw_bytes_read": 0,
            "raw_rebuild_or_download": False,
            "required_followup":
                "EXACT_S3_VERSION_SIZE_AND_SHA256_VERIFICATION",
            "s3_reads": 0,
            "s3_writes": 0,
        }, sort_keys=True))
        return 0
    if args.command == "freeze-forward":
        try:
            path, descriptor = freeze_forward_auxiliary_set(
                args.date, args.bucket, args.prefix, raw_root, warehouse_root,
                quality_dir, os.path.abspath(args.aux_root))
        except cr.ReceiptError as exc:
            print("BLOCKED_INTEGRITY %s" % exc, file=os.sys.stderr)
            return 2
        print(json.dumps({
            "state": "FORWARD_AUXILIARY_SET_FROZEN",
            "date": args.date,
            "path": path,
            "aux_set_sha256": descriptor["aux_set_sha256"],
            "catalog_bytes_copied": 0,
            "dim_bytes_copied": 0,
            "s3_writes": 0,
        }, sort_keys=True))
        return 0
    try:
        seal_binding, objects = build_forward_inventory(
            args.date, args.bucket, args.prefix, raw_root, warehouse_root,
            quality_dir, os.path.abspath(args.aux_bundle),
            os.path.abspath(args.version_binding),
            fresh_rfq_eligibility=(
                os.path.abspath(args.fresh_rfq_eligibility)
                if args.fresh_rfq_eligibility else None))
    except cr.ReceiptError as exc:
        print("BLOCKED_INTEGRITY %s" % exc, file=os.sys.stderr)
        return 2
    if args.command == "plan-forward":
        print(json.dumps({
            "state": "FORWARD_INVENTORY_PLANNED",
            "date": args.date,
            "objects": len(objects),
            "bytes": sum(row["size"] for row in objects),
            "research_candidates": sum(
                row["research_candidate"] for row in objects),
            "s3_writes": 0,
        }, sort_keys=True))
        return 0
    client = cr.AwsCliS3Client(args.aws_cli)
    output_root = os.path.abspath(args.output_root)
    verified, failures, complete = cr.verify_inventory(
        objects, client, os.path.join(output_root, ".tmp"),
        metadata_only=args.metadata_only, probe_limit=args.probe_limit,
        workers=args.workers)
    receipt = None
    state = "BLOCKED_INTEGRITY" if failures else \
        "METADATA_PREFLIGHT_VERIFIED" if args.metadata_only and \
        len(verified) == len(objects) else \
        "PENDING_CONTENT_VERIFICATION" if not complete else "CONTENT_VERIFIED"
    if state == "CONTENT_VERIFIED":
        try:
            receipt = cr.write_shadow_receipt(
                output_root, args.date, seal_binding, verified,
                cr._now(), cr._code_commit())
            state = "RECEIPT_VERIFIED_SHADOW"
        except cr.ReceiptError as exc:
            failures.append({"code": exc.code, "detail": exc.detail})
            state = "BLOCKED_INTEGRITY"
    status = {
        "schema_version": cr.SHADOW_STATUS_SCHEMA,
        "state": state,
        "date": args.date,
        "inventory_objects": len(objects),
        "inventory_bytes": sum(row["size"] for row in objects),
        "verified_objects": sum(
            row.get("durability_verified") is True for row in verified),
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
    cr.write_atomic_json(status_path, status)
    print(json.dumps({
        "state": state,
        "status": str(status_path),
        "receipt": str(receipt) if receipt else None,
        "failures": len(failures),
    }, sort_keys=True))
    return 0 if state in (
        "RECEIPT_VERIFIED_SHADOW", "METADATA_PREFLIGHT_VERIFIED") else 2


if __name__ == "__main__":
    raise SystemExit(main())
