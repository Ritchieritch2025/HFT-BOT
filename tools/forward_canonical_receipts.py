#!/usr/bin/env python3
"""Forward-only canonical receipt preparation for new sealed dates.

This module complements :mod:`canonical_receipts`.  The older ``freeze-aux``
path is intentionally a migration path: it proves that a copied v2 catalog
matches an historical canonical version.  New dates do not need that bridge.
They freeze the local catalog bytes at publication time, then bind the S3
version returned by HEAD and verify that exact version byte-for-byte.

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
import publication_generation as pg
import research_release
import warehouse_common as wc


FORWARD_AUX_SCHEMA = "canonical-forward-auxiliary-set-v1"
FORWARD_CATALOG_PROVENANCE = "FORWARD_LOCAL_SNAPSHOT_PENDING_EXACT_VERSION"
FORWARD_LARGE_ATTESTATION = "LOCAL_LARGE_CANONICAL_ATTESTATION"
DAY_US = 86_400_000_000


def _generation_lock_timeout():
    raw = os.environ.get("HFT_GENERATION_LOCK_TIMEOUT_S", "5")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise cr.ReceiptError(
            "GENERATION_LOCK_INVALID", "invalid lock timeout %r" % raw)
    if value < 0 or value > 300:
        raise cr.ReceiptError(
            "GENERATION_LOCK_INVALID", "lock timeout out of range")
    return value


def _generation_binding(manifest, state):
    return {
        "schema_version": manifest["schema_version"],
        "group": manifest["group"],
        "date": manifest["date"],
        "generation_id": manifest["generation_id"],
        "source_catalog_generation_id":
            manifest["source_catalog_generation_id"],
        "files": copy.deepcopy(manifest["files"]),
        "manifest_state": state,
    }


def _validate_generation_binding(binding, group, date):
    if not isinstance(binding, dict) or set(binding) != {
            "schema_version", "group", "date", "generation_id",
            "source_catalog_generation_id", "files", "manifest_state"}:
        raise cr.ReceiptError(
            "AUX_SET_INVALID", "%s generation binding shape invalid" % group)
    state = binding["manifest_state"]
    if state not in {
            "PRODUCER_MANIFEST_VERIFIED",
            "LEGACY_SYNTHESIZED_UNDER_SHARED_LOCK"}:
        raise cr.ReceiptError(
            "AUX_SET_INVALID", "%s generation state invalid" % group)
    manifest = {key: copy.deepcopy(binding[key]) for key in (
        "schema_version", "group", "date", "generation_id",
        "source_catalog_generation_id", "files")}
    try:
        rebuilt = pg.build_manifest(
            group, manifest["files"], date=date,
            source_catalog_generation_id=manifest[
                "source_catalog_generation_id"])
    except pg.GenerationError as exc:
        raise cr.ReceiptError("AUX_SET_INVALID", str(exc))
    if manifest != rebuilt or binding["group"] != group or binding["date"] != date:
        raise cr.ReceiptError(
            "AUX_SET_INVALID", "%s generation identity invalid" % group)
    if any(not row["relative_path"].startswith(group + "/")
           for row in binding["files"]):
        raise cr.ReceiptError(
            "AUX_SET_INVALID", "%s generation path invalid" % group)
    return binding


def _catalog_projection(rows):
    out = [{
        "bucket": row["bucket"],
        "key": row["key"],
        "logical_source_key": row["logical_source_key"],
        "size": row["size"],
        "sha256": row["sha256"],
    } for row in rows]
    out.sort(key=lambda row: row["logical_source_key"])
    return out


def _freeze_forward_catalog(date, bucket, prefix, warehouse_root):
    root = pathlib.Path(warehouse_root) / "catalog"
    allowed = set(cr.CATALOG_REQUIRED + cr.CATALOG_OPTIONAL)
    actual = set()
    if root.is_symlink():
        raise cr.ReceiptError(
            "FORWARD_CATALOG_INVALID", "catalog root is a symlink")
    if root.is_dir():
        for base, dirs, files in os.walk(root):
            dirs.sort()
            for name in dirs:
                full = pathlib.Path(base) / name
                if full.is_symlink():
                    raise cr.ReceiptError(
                        "FORWARD_CATALOG_INVALID",
                        "catalog directory symlink: %s" % full)
            for name in sorted(files):
                full = pathlib.Path(base) / name
                if full.is_symlink():
                    raise cr.ReceiptError(
                        "FORWARD_CATALOG_INVALID", "catalog symlink: %s" % full)
                actual.add(os.path.relpath(full, root).replace(os.sep, "/"))
    missing = set(cr.CATALOG_REQUIRED) - actual
    unexpected = actual - allowed
    if missing or unexpected:
        raise cr.ReceiptError(
            "FORWARD_CATALOG_INVALID",
            "catalog allowlist mismatch missing=%s unexpected=%s" %
            (sorted(missing), sorted(unexpected)))

    rows = []
    for rel in sorted(actual):
        path = root.joinpath(*rel.split("/"))
        size, digest = cr._file_attestation(str(path))
        rows.append({
            "storage_mode": "LOCAL_HASH_ATTESTATION",
            "local_relpath": None,
            "bucket": bucket,
            "key": cr._join_key(prefix, "warehouse", "catalog", rel),
            "logical_source_key": cr._join_key("warehouse", "catalog", rel),
            "source_kind": "catalog",
            "family": "catalog_at_publication",
            "table": rel.split("/", 1)[0],
            "channel": None,
            "date": date,
            "size": size,
            "sha256": digest,
            "seal_binding": None,
            "evidence_binding": None,
            "required": True,
            "durability_scope": True,
            "research_candidate": True,
            "exposure_policy": "PENDING_ELIGIBILITY_TAG",
            "version_resolution": "FORWARD_CURRENT_EXACT",
            "canonical_source": "LOCAL_SNAPSHOT_THEN_EXACT_S3_VERSION",
            "expected_version_id": None,
        })
    generation_paths = ["catalog/" + rel for rel in sorted(actual)]
    generation_files = sorted([{
        "relative_path": "catalog/" + row["logical_source_key"][len(
            "warehouse/catalog/"):],
        "size": row["size"],
        "sha256": row["sha256"],
    } for row in rows], key=lambda row: row["relative_path"])
    try:
        generation = pg.load_manifest(
            warehouse_root, "catalog", expected_paths=generation_paths,
            verify_files=False, allow_missing=True)
        if generation is None:
            generation = pg.build_manifest("catalog", generation_files)
            generation_state = "LEGACY_SYNTHESIZED_UNDER_SHARED_LOCK"
        else:
            generation_state = "PRODUCER_MANIFEST_VERIFIED"
            if generation["files"] != generation_files:
                raise pg.GenerationError(
                    "catalog bytes do not match producer generation")
    except pg.GenerationError as exc:
        raise cr.ReceiptError("FORWARD_CATALOG_GENERATION_INVALID", str(exc))
    catalog_sha = cr.canonical_sha256(_catalog_projection(rows))
    for row in rows:
        row["evidence_binding"] = {
            "provenance": FORWARD_CATALOG_PROVENANCE,
            "catalog_set_sha256": catalog_sha,
            "catalog_generation_id": generation["generation_id"],
        }
    return rows, catalog_sha, _generation_binding(
        generation, generation_state)


def _freeze_forward_dim_generation(date, warehouse_root,
                                   catalog_generation_id):
    paths = [
        "dim/snapshots/date=%s/%s" % (date, name)
        for name in cr.DIM_REQUIRED]
    try:
        generation, state = pg.verified_or_synthesized_manifest(
            warehouse_root, "dim", paths, date=date,
            source_catalog_generation_id=catalog_generation_id)
    except pg.GenerationError as exc:
        raise cr.ReceiptError("FORWARD_DIM_GENERATION_INVALID", str(exc))
    if generation["source_catalog_generation_id"] != catalog_generation_id:
        raise cr.ReceiptError(
            "FORWARD_GENERATION_MISMATCH",
            "dated dim generation was built from catalog %s, current is %s" %
            (generation["source_catalog_generation_id"],
             catalog_generation_id))
    binding = _generation_binding(generation, state)
    return binding, cr.canonical_sha256(generation["files"])


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
    """Freeze small controls plus hash-only catalog attestations.

    Catalog bytes are read once through a pinned fd and hashed, but are not
    copied into the bundle.  A later shadow run must prove that the canonical
    S3 exact version has the same size and SHA-256.
    """
    cr._validate_date(date)
    if not bucket or "/" in bucket:
        raise cr.ReceiptError("INVALID_BUCKET", repr(bucket))
    prefix = cr._safe_rel(prefix.strip("/"), "canonical prefix")
    (seal, binding, manifest_payload, manifest_digest, _manifest_rows,
     _raw_proofs, fact_proofs) = cr._authoritative_day_inputs(
         date, raw_root, warehouse_root)
    seal_sha = binding["sha256"]

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

        try:
            with pg.generation_locks(
                    warehouse_root,
                    {"catalog": "shared", "dim": "shared"},
                    timeout=_generation_lock_timeout()):
                catalog_rows, catalog_sha, catalog_generation = (
                    _freeze_forward_catalog(
                        date, bucket, prefix, warehouse_root))
                dim_generation, dim_sha = _freeze_forward_dim_generation(
                    date, warehouse_root,
                    catalog_generation["generation_id"])
        except pg.GenerationError as exc:
            raise cr.ReceiptError("GENERATION_BUSY", str(exc))
        for row in catalog_rows:
            row["seal_binding"] = seal_sha
        objects.extend(catalog_rows)

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

        l2_required = any(proof.get("table") == "orderbooks_full"
                          for proof in fact_proofs)
        l2_path = os.path.join(quality_dir, "l2_gaps_%s.json" % date)
        if l2_required and not os.path.isfile(l2_path):
            raise cr.ReceiptError(
                "AUX_INPUT_INCOMPLETE", "L2 quality receipt is missing")
        if os.path.isfile(l2_path):
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
            "catalog_snapshot": {
                "provenance": FORWARD_CATALOG_PROVENANCE,
                "catalog_set_sha256": catalog_sha,
                "generation": catalog_generation,
            },
            "dim_snapshot": {
                "dim_set_sha256": dim_sha,
                "generation": dim_generation,
            },
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
        snapshot = descriptor.get("catalog_snapshot")
        if (not isinstance(snapshot, dict)
                or set(snapshot) != {
                    "provenance", "catalog_set_sha256", "generation"}
                or snapshot.get("provenance") != FORWARD_CATALOG_PROVENANCE
                or not cr.SHA256_RE.match(
                    str(snapshot.get("catalog_set_sha256") or ""))):
            raise cr.ReceiptError(
                "AUX_SET_INVALID", "catalog snapshot metadata invalid")
        catalog_generation = _validate_generation_binding(
            snapshot.get("generation"), "catalog", None)
        dim_snapshot = descriptor.get("dim_snapshot")
        if (not isinstance(dim_snapshot, dict)
                or set(dim_snapshot) != {"dim_set_sha256", "generation"}
                or not cr.SHA256_RE.match(
                    str(dim_snapshot.get("dim_set_sha256") or ""))):
            raise cr.ReceiptError(
                "AUX_SET_INVALID", "dim snapshot metadata invalid")
        dim_generation = _validate_generation_binding(
            dim_snapshot.get("generation"), "dim", date)
        if (dim_generation["source_catalog_generation_id"]
                != catalog_generation["generation_id"]
                or cr.canonical_sha256(dim_generation["files"])
                != dim_snapshot["dim_set_sha256"]):
            raise cr.ReceiptError(
                "AUX_SET_INVALID", "catalog/dim generation binding mismatch")
        projection = {key: value for key, value in descriptor.items()
                      if key != "aux_set_sha256"}
        digest = cr.canonical_sha256(projection)
        if (descriptor.get("aux_set_sha256") != digest
                or pathlib.Path(path).name != "aux-set=%s" % digest):
            raise cr.ReceiptError("AUX_SET_INVALID", "bundle digest mismatch")

        allowed_families = {
            "manifest_date_projection", "catalog_at_publication",
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
            if storage_mode == "LOCAL_HASH_ATTESTATION":
                if (row.get("family") != "catalog_at_publication"
                        or local_rel is not None
                        or row.get("expected_version_id") is not None):
                    raise cr.ReceiptError(
                        "AUX_SET_INVALID", "catalog attestation invalid")
            elif storage_mode == FORWARD_LARGE_ATTESTATION:
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
        catalog = by_family.get("catalog_at_publication", [])
        rels = set()
        for row in catalog:
            marker = "warehouse/catalog/"
            if not row["logical_source_key"].startswith(marker):
                raise cr.ReceiptError(
                    "AUX_SET_INVALID", "catalog logical key invalid")
            rel = row["logical_source_key"][len(marker):]
            rels.add(rel)
            if (row["key"] != cr._join_key(
                    prefix, "warehouse", "catalog", rel)
                    or row.get("source_kind") != "catalog"
                    or row.get("table") != rel.split("/", 1)[0]
                    or row.get("channel") is not None
                    or row.get("version_resolution")
                    != "FORWARD_CURRENT_EXACT"
                    or row.get("canonical_source")
                    != "LOCAL_SNAPSHOT_THEN_EXACT_S3_VERSION"
                    or row.get("evidence_binding") != {
                        "provenance": FORWARD_CATALOG_PROVENANCE,
                        "catalog_set_sha256":
                            snapshot["catalog_set_sha256"],
                        "catalog_generation_id":
                            catalog_generation["generation_id"],
                    }):
                raise cr.ReceiptError(
                    "AUX_SET_INVALID", "catalog semantics invalid")
        if (not set(cr.CATALOG_REQUIRED) <= rels
                or not rels <= set(cr.CATALOG_REQUIRED + cr.CATALOG_OPTIONAL)
                or cr.canonical_sha256(_catalog_projection(catalog))
                != snapshot["catalog_set_sha256"]):
            raise cr.ReceiptError(
                "AUX_SET_INVALID", "catalog set mismatch")
        catalog_generation_files = sorted([{
            "relative_path": "catalog/" +
                row["logical_source_key"][len("warehouse/catalog/"):],
            "size": row["size"],
            "sha256": row["sha256"],
        } for row in catalog], key=lambda row: row["relative_path"])
        if catalog_generation_files != catalog_generation["files"]:
            raise cr.ReceiptError(
                "AUX_SET_INVALID", "catalog generation/files mismatch")
        expected_dim_paths = {
            "dim/snapshots/date=%s/%s" % (date, name)
            for name in cr.DIM_REQUIRED}
        if {row["relative_path"] for row in dim_generation["files"]} \
                != expected_dim_paths:
            raise cr.ReceiptError(
                "AUX_SET_INVALID", "dim generation member set mismatch")

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

        l2_required = any(row.get("table") == "orderbooks_full"
                          for row in seal.get("archive_file_stats", []))
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


def _revalidate_forward_corrections(date, warehouse_root, auxiliary):
    """Rebind the frozen large attestation to current local canonical bytes."""
    corr = [row for row in auxiliary
            if row.get("family") == "corrections_at_cutoff"]
    late = next((row for row in corr
                 if row.get("source_kind") == "correction"), None)
    ledger = next((row for row in corr
                   if row.get("source_kind")
                   == "corrections_ledger_day"), None)
    late_path = os.path.join(
        warehouse_root, "corrections", "date=%s" % date,
        "late_rows.ndjson")
    ledger_path = os.path.join(
        warehouse_root, "corrections", "ledger.ndjson")
    late_present = os.path.lexists(late_path)
    ledger_present = os.path.lexists(ledger_path)
    try:
        if ledger_present:
            ledger_day, ledger_rows, ledger_fingerprint = (
                cr._ledger_projection_file(ledger_path, date))
        else:
            ledger_day, ledger_rows, ledger_fingerprint = b"", [], None
        if (late is None) != (ledger is None):
            raise cr.ReceiptError(
                "FORWARD_CORRECTIONS_CHANGED",
                "frozen correction pair is incomplete")
        if late is None:
            if late_present or ledger_rows:
                raise cr.ReceiptError(
                    "FORWARD_CORRECTIONS_CHANGED",
                    "corrections appeared after the forward freeze")
            if ledger_fingerprint is not None:
                cr._assert_fingerprint(ledger_path, ledger_fingerprint)
            return
        if not late_present or not ledger_rows:
            raise cr.ReceiptError(
                "FORWARD_CORRECTIONS_CHANGED",
                "corrections disappeared after the forward freeze")
        (late_size, late_sha, validation,
         late_fingerprint) = cr._late_correction_file_attestation(
             late_path, ledger_rows, date)
        ledger_sha = hashlib.sha256(ledger_day).hexdigest()
        evidence = {
            "ledger_day_sha256": ledger_sha,
            "late_rows_validation": validation,
        }
        if (late.get("size") != late_size
                or late.get("sha256") != late_sha
                or late.get("evidence_binding") != evidence
                or ledger.get("size") != len(ledger_day)
                or ledger.get("sha256") != ledger_sha
                or ledger.get("evidence_binding") != evidence
                or ledger.get("_local_payload") != ledger_day):
            raise cr.ReceiptError(
                "FORWARD_CORRECTIONS_CHANGED",
                "canonical correction bytes differ from the frozen set")
        cr._assert_fingerprint(ledger_path, ledger_fingerprint)
        cr._assert_fingerprint(late_path, late_fingerprint)
    except cr.ReceiptError as exc:
        if exc.code == "FORWARD_CORRECTIONS_CHANGED":
            raise
        raise cr.ReceiptError("FORWARD_CORRECTIONS_CHANGED", exc.detail)


def build_forward_inventory(date, bucket, prefix, raw_root, warehouse_root,
                            quality_dir, aux_bundle,
                            include_rfq_durability=False):
    """Build the forward desired set without making an S3 call.

    RFQ is excluded from the default byte-verification set under the operator's
    closed/no-repair ruling.  The explicit function-only capability remains
    for a future independently authorized RFQ receipt; no CLI flag exposes it
    in the current core workflow.
    """
    if not bucket or "/" in bucket:
        raise cr.ReceiptError("INVALID_BUCKET", repr(bucket))
    prefix = cr._safe_rel(prefix.strip("/"), "canonical prefix")
    (seal, binding, _manifest_payload, manifest_digest, _manifest_rows,
     raw_proofs, fact_proofs) = cr._authoritative_day_inputs(
         date, raw_root, warehouse_root)
    seal_sha = binding["sha256"]
    objects = []
    seen = {"physical": set(), "logical": set()}
    families = []

    included_raw_proofs = []
    deferred_rfq_proofs = []
    for proof in sorted(raw_proofs, key=lambda item: item["file"]):
        rel = proof["file"]
        path_date, basename = cr._raw_rel_parts(rel)
        kind, channel = cr._raw_classification(rel)
        is_rfq = kind in {"raw_rfq", "raw_rfq_receipts"}
        if is_rfq and not include_rfq_durability:
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
            version_resolution="SEALED_CURRENT_EXACT")
        cr._add_unique(objects, seen, obj)
    families.append(cr._family(
        "raw_durability", "REQUIRED_CORE", "seal.raw_files",
        len(included_raw_proofs), len(included_raw_proofs),
        "PRESENT_VERIFIED",
        semantic_sha256=cr.canonical_sha256(included_raw_proofs)))
    if deferred_rfq_proofs:
        families.append(cr._family(
            "raw_rfq_deferred", "CONDITIONAL",
            "operator RFQ branch closed; separate future receipt required",
            0, 0, "NOT_APPLICABLE", "RFQ_BRANCH_CLOSED_NO_REPAIR",
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

    dim_root = os.path.join(
        warehouse_root, "dim", "snapshots", "date=%s" % date)
    dim_present = 0
    try:
        with pg.generation_locks(
                warehouse_root,
                {"catalog": "shared", "dim": "shared"},
                timeout=_generation_lock_timeout()):
            (_catalog_rows, current_catalog_sha,
             current_catalog_generation) = _freeze_forward_catalog(
                 date, bucket, prefix, warehouse_root)
            current_dim_generation, current_dim_sha = (
                _freeze_forward_dim_generation(
                    date, warehouse_root,
                    current_catalog_generation["generation_id"]))
            descriptor, auxiliary = load_forward_auxiliary_set(
                aux_bundle, date, bucket, prefix, seal, seal_sha)
            current_catalog_snapshot = {
                "provenance": FORWARD_CATALOG_PROVENANCE,
                "catalog_set_sha256": current_catalog_sha,
                "generation": current_catalog_generation,
            }
            current_dim_snapshot = {
                "dim_set_sha256": current_dim_sha,
                "generation": current_dim_generation,
            }
            if (descriptor["catalog_snapshot"] != current_catalog_snapshot
                    or descriptor["dim_snapshot"] != current_dim_snapshot):
                raise cr.ReceiptError(
                    "FORWARD_GENERATION_CHANGED",
                    "catalog or dated dim changed after forward freeze")
            for rel in cr.DIM_REQUIRED:
                path = os.path.join(dim_root, rel)
                if not os.path.isfile(path):
                    continue
                dim_present += 1
                cr._add_local_file(
                    objects, seen, bucket=bucket,
                    key=cr._join_key(
                        prefix, "warehouse", "dim", "snapshots",
                        "date=%s" % date, rel),
                    logical=cr._join_key(
                        "warehouse", "dim", "snapshots",
                        "date=%s" % date, rel),
                    kind="dim_snapshot", date=date, seal_sha=None, path=path,
                    family="dim_snapshot",
                    attestation_class="PUBLICATION_CUTOFF_FROZEN",
                    durability_scope=True, research_candidate=True,
                    exposure_policy="PENDING_ELIGIBILITY_TAG",
                    version_resolution="WRITE_ONCE_CURRENT")
    except pg.GenerationError as exc:
        raise cr.ReceiptError("GENERATION_BUSY", str(exc))
    _revalidate_forward_corrections(date, warehouse_root, auxiliary)
    families.append(cr._family(
        "dim_snapshot", "REQUIRED_RESEARCH", "fixed dated dim contract",
        len(cr.DIM_REQUIRED), dim_present,
        "PRESENT_VERIFIED" if dim_present == len(cr.DIM_REQUIRED)
        else "INCOMPLETE",
        "NONE" if dim_present == len(cr.DIM_REQUIRED)
        else "DIM_FILES_MISSING",
        semantic_sha256=cr.canonical_sha256(descriptor["dim_snapshot"])))

    aux_by_family = {}
    for row in auxiliary:
        aux_by_family.setdefault(row["family"], []).append(row)
        obj = cr._entry(
            row["bucket"], row["key"], row["logical_source_key"],
            row["source_kind"], row["date"], row["size"], row["sha256"],
            seal_sha, table=row.get("table"), channel=row.get("channel"),
            local_path=row.get("_local_path"),
            local_payload=row.get("_local_payload"),
            evidence_binding=row.get("evidence_binding"),
            mutable_source=False,
            required=True, family=row["family"],
            attestation_class="PUBLICATION_CUTOFF_FROZEN",
            durability_scope=True,
            research_candidate=row["research_candidate"],
            exposure_policy=row["exposure_policy"],
            version_resolution=row["version_resolution"],
            canonical_source=row["canonical_source"])
        cr._add_unique(objects, seen, obj)

    families.append(cr._family(
        "manifest_date_projection", "REQUIRED_CORE",
        "date-only seal-bound manifest projection", 1,
        len(aux_by_family.get("manifest_date_projection", [])),
        "PRESENT_VERIFIED", semantic_sha256=manifest_digest))
    catalog = aux_by_family.get("catalog_at_publication", [])
    families.append(cr._family(
        "catalog_at_publication", "REQUIRED_RESEARCH",
        "forward local snapshot matched to exact canonical version",
        len(catalog), len(catalog), "PRESENT_VERIFIED",
        semantic_sha256=descriptor["catalog_snapshot"][
            "catalog_set_sha256"]))
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
    l2_required = any(proof.get("table") == "orderbooks_full"
                      for proof in fact_proofs)
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
    freeze = sub.add_parser("freeze-forward")
    _common_args(freeze)
    freeze.add_argument(
        "--aux-root", default=os.path.join(
            wc.ROOT, "work", "live", "canonical_receipts", "forward-aux"))
    plan = sub.add_parser("plan-forward")
    _common_args(plan)
    plan.add_argument("--aux-bundle", required=True)
    shadow = sub.add_parser("shadow-forward")
    _common_args(shadow)
    shadow.add_argument("--aux-bundle", required=True)
    shadow.add_argument("--aws-cli", default="aws")
    shadow.add_argument("--metadata-only", action="store_true")
    shadow.add_argument("--probe-limit", type=int)
    shadow.add_argument(
        "--output-root", default=os.path.join(
            wc.ROOT, "work", "live", "canonical_receipts", "forward-shadow"))
    args = parser.parse_args(argv)
    cfg = wc.load_config()
    raw_root = os.path.abspath(args.raw_root or cfg["raw_root"])
    warehouse_root = os.path.abspath(
        args.warehouse_root or cfg["warehouse_root"])
    quality_dir = os.path.abspath(args.quality_dir)
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
            "s3_writes": 0,
        }, sort_keys=True))
        return 0
    try:
        seal_binding, objects = build_forward_inventory(
            args.date, args.bucket, args.prefix, raw_root, warehouse_root,
            quality_dir, os.path.abspath(args.aux_bundle))
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
        metadata_only=args.metadata_only, probe_limit=args.probe_limit)
    receipt = None
    state = "BLOCKED_INTEGRITY" if failures else \
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
    return 0 if state == "RECEIPT_VERIFIED_SHADOW" else 2


if __name__ == "__main__":
    raise SystemExit(main())
