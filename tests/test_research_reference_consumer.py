#!/usr/bin/env python3
"""Offline contract tests for the copied-v2/reference-v3 dual consumer."""
import copy
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import research_data as rd  # noqa: E402
import research_reference as ref  # noqa: E402


DATE = "2026-07-13"
LATE_RECEIPT_DATE = "2026-07-15"
BUCKET = ref.TRUSTED_BUCKET
FACT_COLUMNS = [
    "market_ticker", "ts_utc", "exchange_ts_us", "recv_wall_ns",
    "recv_mono_ns", "local_recv_ts_us", "ws_sid", "ws_seq",
]
FROZEN_COLUMNS = FACT_COLUMNS + ["category", "date", "subcategory"]


def _sha(payload):
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, indent=2).encode() + b"\n"


def _pin_legacy_manifest(manifest):
    pin = (manifest["release_id"], ref.canonical_sha256(manifest),
           manifest["published_at_utc"])
    ref.LEGACY_V3_MANIFEST_PINS = frozenset(
        set(ref.LEGACY_V3_MANIFEST_PINS) | {pin})


def _manifest_csv(facts):
    fields = (
        "date", "table", "category", "subcategory", "row_count",
        "file_path", "file_md5", "created_ts",
    )
    rows = []
    for table, rel, _payload in facts:
        rows.append({
            "date": DATE,
            "table": table,
            "category": "Sports",
            "subcategory": "Baseball",
            "row_count": "1",
            "file_path": "warehouse/facts/%s" % rel,
            "file_md5": "0" * 32,
            "created_ts": "2026-07-14T03:00:00Z",
        })
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode(), rows


def build_release(*, with_rfq=False, receipt_variant="fixture",
                  rfq_date=DATE, legacy=False, pin_legacy=True):
    line = ",".join(FACT_COLUMNS) + "\nTICKER,1,1,1,1,1,1,1\n"
    fact_payload = line.encode()
    facts = []
    for table in ("orderbooks_l1", "orderbooks_full", "trades"):
        rel = ("%s/category=Sports/subcategory=Baseball/date=%s/part.csv"
               % (table, DATE))
        facts.append((table, rel, fact_payload))

    manifest_payload, manifest_rows = _manifest_csv(facts)
    manifest_digest = ref.canonical_sha256(sorted(
        manifest_rows,
        key=lambda row: (row["table"], row["category"],
                         row["subcategory"], row["file_path"]),
    ))
    raw_proofs = [
        {"file": "date=%s/firehose_23.ndjson" % DATE,
         "size": 17, "sha256": "1" * 64},
        {"file": "date=%s/l2_23.ndjson" % DATE,
         "size": 19, "sha256": "2" * 64},
    ]
    rfq_payload = b'{"rfq":"sealed"}\n'
    rfq_receipts_payload = b'{"rfq_receipt":"sealed"}\n'
    if with_rfq:
        raw_proofs.append({
            "file": "date=%s/rfq_23.ndjson.2" % rfq_date,
            "size": len(rfq_payload), "sha256": _sha(rfq_payload),
        })
        raw_proofs.append({
            "file": "date=%s/rfq_receipts_23.ndjson" % rfq_date,
            "size": len(rfq_receipts_payload),
            "sha256": _sha(rfq_receipts_payload),
        })
    archive = [{
        "file": rel, "table": table, "size": len(payload),
        "sha256": _sha(payload), "rows": 1,
    } for table, rel, payload in facts]
    seal = {
        "date": DATE,
        "status": "SEALED",
        "version": 2,
        "method": "full_v2",
        "go_no_go_eligible": True,
        "capture_quality_status": "ASSESSED_PASS",
        "manifest_date_sha256": manifest_digest,
        "archive_file_stats": archive,
        "archive_files": len(archive),
        "archive_rows": len(archive),
        "raw_files": raw_proofs,
        "sealed_at": "2026-07-14T03:10:00Z",
    }
    seal_payload = _json_bytes(seal)
    seal_sha = _sha(seal_payload)

    capture = {
        "schema_version": "capture-gap-scan-receipt-v1",
        "date": DATE,
        "files": [{"file": raw_proofs[0]["file"], "bytes": 17}],
        "n_files": 1,
        "total_bytes": 17,
        "records": 3,
        "unparsed": 0,
        "unreadable": False,
        "gaps": [],
    }
    capture_payload = _json_bytes(capture)
    capture_gaps_payload = b"start_us,end_us\n"
    l2 = {
        "schema_version": "l2-gap-receipt-v1",
        "date": DATE,
        "no_l2_files": False,
        "file_inventory": [{"file": raw_proofs[1]["file"], "bytes": 19}],
        "lines": 4,
        "parse_errors": 0,
        "seq_gap_events": 0,
        "seq_missed_total": 0,
        "sids_total": 1,
        "sids_with_seq_gaps": 0,
    }
    l2_payload = _json_bytes(l2)
    l2_quality = {key: l2[key] for key in (
        "no_l2_files", "lines", "parse_errors", "seq_gap_events",
        "seq_missed_total", "sids_total", "sids_with_seq_gaps",
    )}

    objects = []
    source_bytes = {}

    def add(logical, source, payload, kind, channel=None, required=True,
            seal_binding=seal_sha, evidence_binding="fixture-evidence"):
        item = {
            "logical_key": logical,
            "source_bucket": BUCKET,
            "source_key": source,
            "source_version_id": "version-%s" % _sha(source.encode())[:20],
            "size": len(payload),
            "sha256": _sha(payload),
            "kind": kind,
            "channel": channel,
            "date": (logical.split("date=", 1)[1].split("/", 1)[0]
                     if logical.startswith("raw_rfq/date=") else DATE),
            "required": required,
            "seal_binding": seal_binding,
            "evidence_binding": evidence_binding,
            "source_last_modified_utc": "2026-07-13T04:00:00Z",
        }
        objects.append(item)
        source_bytes[(BUCKET, source, item["source_version_id"])] = payload
        return item

    for table, rel, payload in facts:
        add("warehouse/facts/%s" % rel, "ec2/warehouse/facts/%s" % rel,
            payload, "facts", table,
            evidence_binding="seal.archive_file_stats")
    seal_item = add(
        "warehouse/seals/date=%s.json" % DATE,
        "ec2/warehouse/seals/date=%s.json" % DATE,
        seal_payload, "seal", evidence_binding="self")
    manifest_sha = _sha(manifest_payload)
    add(
        "warehouse/manifest.csv",
        ("ec2/warehouse/publication-snapshots/v1/date=%s/manifest/"
         "sha256=%s/manifest.csv") % (DATE, manifest_sha),
        manifest_payload, "warehouse_manifest_day",
        evidence_binding={"manifest_date_sha256": manifest_digest})
    for name in ("series.csv", "events.csv", "markets.csv"):
        payload = ("id,value\n%s,fixture\n" % name).encode()
        add(
            "warehouse/dim/snapshots/date=%s/%s" % (DATE, name),
            "ec2/warehouse/dim/snapshots/date=%s/%s" % (DATE, name),
            payload, "dim_snapshot", seal_binding=None, evidence_binding=None)
    for name in ("series", "events", "markets"):
        payload = ("catalog-%s" % name).encode()
        rel = "%s/part-00000.parquet" % name
        add("warehouse/catalog/%s" % rel,
            "ec2/warehouse/catalog/%s" % rel,
            payload, "catalog", evidence_binding={
                "catalog_set_sha256": "3" * 64,
                "cutoff_utc": "2026-07-14T03:00:00Z",
            })
    gap_item = add(
        "control/quality/v1/date=%s/capture_gap_receipt.json" % DATE,
        ("ec2/control/quality/v1/date=%s/capture_gap_receipt/"
         "sha256=%s/capture_gap_receipt.json") %
        (DATE, _sha(capture_payload)),
        capture_payload, "capture_gap_receipt",
        evidence_binding="sealed firehose inventory")
    gap_projection_item = add(
        "control/quality/v1/date=%s/capture_gaps.csv" % DATE,
        ("ec2/control/quality/v1/date=%s/capture_gaps/"
         "sha256=%s/capture_gaps.csv") %
        (DATE, _sha(capture_gaps_payload)),
        capture_gaps_payload, "capture_gaps_projection",
        evidence_binding="capture receipt gap intervals")
    l2_item = add(
        "control/quality/v1/date=%s/l2_gaps.json" % DATE,
        ("ec2/control/quality/v1/date=%s/l2_gaps/"
         "sha256=%s/l2_gaps.json") % (DATE, _sha(l2_payload)),
        l2_payload, "l2_quality_receipt",
        evidence_binding="sealed l2 inventory")
    rfq_items = []
    if with_rfq:
        rfq_items.append(add(
            "raw_rfq/date=%s/rfq_23.ndjson.2" % rfq_date,
            "ec2/raw/date=%s/rfq_23.ndjson.2" % rfq_date,
            rfq_payload, "rfq", channel="rfq", required=False,
            evidence_binding="seal.raw_files"))
        rfq_items.append(add(
            "raw_rfq/date=%s/rfq_receipts_23.ndjson" % rfq_date,
            "ec2/raw/date=%s/rfq_receipts_23.ndjson" % rfq_date,
            rfq_receipts_payload, "rfq", channel="rfq", required=False,
            evidence_binding="seal.raw_files"))
        rfq_exact_rows = [{
            "bucket": item["source_bucket"],
            "key": item["source_key"],
            "VersionId": item["source_version_id"],
            "size": item["size"],
            "sha256": item["sha256"],
            "last_modified_utc": item["source_last_modified_utc"],
            "logical_source_key": (
                "raw/" + item["logical_key"][len("raw_rfq/"):]),
            "source_kind": (
                "raw_rfq_receipts" if "/rfq_receipts_" in
                item["logical_key"] else "raw_rfq"),
            "channel": "rfq",
        } for item in rfq_items]
        rfq_binding = {
            "schema_version": ref.RFQ_ELIGIBILITY_BINDING_SCHEMA,
            "state": "ELIGIBLE_SEALED_REFERENCE",
            "source": "seal.raw_files",
            "seal_sha256": seal_sha,
            "rfq_exact_set_sha256": ref._rfq_exact_set_sha256(
                rfq_exact_rows),
            "eligibility_evidence_sha256": ref.canonical_sha256({
                "authorization": "fixture-rfq-closed",
                "date": DATE,
                "seal_sha256": seal_sha,
            }),
            "evidence_tier": "SEALED_CONFIRMATION",
            "integrity_state": "PASS",
            "quarantine_state": "CLEAR",
            "repair_branch_state": "CLOSED_NO_REPAIR",
        }
        for item in rfq_items:
            item["evidence_binding"] = copy.deepcopy(rfq_binding)

    objects.sort(key=lambda item: item["logical_key"])
    reference_sha = ref.reference_set_sha256(objects)
    semantics_sha = ref.object_semantics_sha256(objects)
    tables = {table: {
        "columns": list(FROZEN_COLUMNS),
        "sampled_file": "part.csv",
        "tl1_ladder_columns_present": True,
        "ws_sid_present": True,
        "ws_seq_present": True,
    } for table, _rel, _payload in facts}
    channels = {
        "orderbooks_l1": {
            "status": "INCLUDED",
            "completeness": "CONFLATED_CHANGE_STREAM_NEVER_LOSSLESS",
            "gap_receipt_affirmative": True,
        },
        "trades": {"status": "INCLUDED", "identity": "trade_id"},
        "orderbooks_l2": {
            "status": "INCLUDED_SEALED_FACTS",
            "seq_quality": l2_quality,
        },
        "rfq": {
            "status": ("INCLUDED_SEALED_REFERENCE" if with_rfq
                       else "EXCLUDED_EXPLICIT_OPT_IN_REQUIRED"),
        },
    }
    evidence_basis = {
        "seal_status": "SEALED",
        "seal_version": 2,
        "method": "full_v2",
        "go_no_go_eligible": True,
        "capture_quality_status": "ASSESSED_PASS",
        "gap_receipt_affirmative": True,
        "l2_facts_present": True,
        "l2_evidence_ok": True,
        "downgrade_reasons": [],
    }
    legacy_component_corrections = {"files": [], "ledger_day_sha256": None}
    legacy_component_gap = {
        "affirmative_receipt": True,
        "receipt_inventory_matched_seal": True,
        "non_affirmative_reason": None,
        "gap_receipt_sha256": gap_item["sha256"],
        "capture_gaps_sha256": gap_projection_item["sha256"],
        "l2_gaps_sha256": l2_item["sha256"],
    }
    legacy_component_rfq = {
        "included": with_rfq,
        "files": [{
            "file": item["logical_key"][len("raw_rfq/"):],
            "size": item["size"], "sha256": item["sha256"],
        } for item in rfq_items],
    }
    receipt_objects = []
    for item in objects:
        is_rfq = item["kind"] == "rfq"
        receipt_objects.append({
            "bucket": item["source_bucket"],
            "key": item["source_key"],
            "VersionId": item["source_version_id"],
            "size": item["size"],
            "sha256": item["sha256"],
            "last_modified_utc": "2026-07-13T04:00:00Z",
            "logical_source_key": (
                "raw/" + item["logical_key"][len("raw_rfq/"):]
                if is_rfq else item["logical_key"]),
            "source_kind": (
                ("raw_rfq_receipts" if "/rfq_receipts_" in
                 item["logical_key"] else "raw_rfq")
                if is_rfq else item["kind"]),
            "family": "fixture",
            "table": item["channel"] if item["kind"] == "facts" else None,
            "channel": item["channel"],
            "date": item["date"],
            "seal_binding": item["seal_binding"],
            "evidence_binding": item["evidence_binding"],
            "durability_verified": True,
            "research_eligible": True,
            "eligibility_tag_state": (
                "DUAL_TAGGED_VERIFIED" if is_rfq else "TAGGED_VERIFIED"),
            "mutable_source": False,
            "required": item["required"],
            "attestation_class": "PUBLICATION_CUTOFF_FROZEN",
            "durability_scope": True,
            "research_candidate": True,
            "exposure_policy": (
                "RESEARCH_ELIGIBLE_SEALED_RFQ"
                if is_rfq else "RESEARCH_ELIGIBLE"),
            "version_resolution": "EXACT_VERSION",
            "canonical_source": "FIXTURE_CANONICAL",
            "verification_state": "EXACT_VERSION_FULL_SHA256",
        })
    receipt_objects.sort(key=lambda row: (
        row["logical_source_key"], row["bucket"], row["key"]))
    family = {
        "name": "fixture",
        "policy": "REQUIRED_RESEARCH",
        "expected_basis": "fixture-%s" % receipt_variant,
        "expected_count": len(receipt_objects),
        "observed_count": len(receipt_objects),
        "state": "PRESENT_VERIFIED",
        "reason_code": "FIXTURE_COMPLETE",
        "semantic_sha256": ref.canonical_sha256({
            "variant": receipt_variant,
            "objects": len(receipt_objects),
        }),
        "objects_digest": ref.canonical_sha256([{
            "bucket": row["bucket"],
            "key": row["key"],
            "size": row["size"],
            "sha256": row["sha256"],
        } for row in sorted(
            receipt_objects, key=lambda item: (item["bucket"], item["key"]))]),
    }
    receipt = {
        "schema_version": "canonical-object-receipt-v1",
        "state": "DURABLE_RECEIPT_VERIFIED",
        "authority": "CANONICAL_CONTROL_PLANE",
        "authoritative": True,
        "s3_published": True,
        "prune_eligible": False,
        "date": DATE,
        "seal": {
            "date": DATE,
            "status": "SEALED",
            "version": 2,
            "method": "full_v2",
            "bucket": BUCKET,
            "key": seal_item["source_key"],
            "VersionId": seal_item["source_version_id"],
            "size": seal_item["size"],
            "sha256": seal_item["sha256"],
            "manifest_date_sha256": manifest_digest,
        },
        "families": [family],
        "objects": receipt_objects,
    }
    receipt_set_sha = ref.canonical_sha256(
        ref.durable_receipt_projection(receipt))
    receipt["receipt_set_sha256"] = receipt_set_sha
    receipt["durability_set_sha256"] = ref._scoped_receipt_sha256(
        receipt_objects, "durability_scope")
    receipt["research_candidate_set_sha256"] = ref._scoped_receipt_sha256(
        receipt_objects, "research_candidate")
    receipt_payload = _json_bytes(receipt)
    receipt_binding = {
        "bucket": BUCKET,
        "key": ("ec2/control/canonical-receipts/v1/date=%s/receipt-%s.json"
                % (DATE, receipt_set_sha)),
        "version_id": "receipt-version-%s" % _sha(receipt_payload)[:20],
        "size": len(receipt_payload),
        "sha256": _sha(receipt_payload),
    }
    source_bytes[(BUCKET, receipt_binding["key"],
                  receipt_binding["version_id"])] = receipt_payload
    def component_projection(kinds):
        return [{key: item.get(key) for key in (
            "logical_key", "source_bucket", "source_key",
            "source_version_id", "size", "sha256", "kind", "channel",
            "evidence_binding")}
                for item in objects if item.get("kind") in kinds]

    correction_refs = component_projection({
        "correction", "corrections_ledger_day"})
    gap_refs = component_projection({
        "capture_gap_receipt", "capture_gaps_projection"})
    l2_refs = component_projection({"l2_quality_receipt"})
    if legacy:
        component_corrections = legacy_component_corrections
        component_gap = legacy_component_gap
        component_l2 = l2_quality
        component_rfq = legacy_component_rfq
        corrections_digest = ref.canonical_sha256(component_corrections)
        gap_digest = ref.canonical_sha256(component_gap)
        l2_digest = ref.canonical_sha256(component_l2)
    else:
        component_corrections = {"objects": correction_refs}
        component_gap = {
            "affirmative_receipt": bool(gap_refs),
            "objects": gap_refs,
        }
        component_l2 = {
            "affirmative_receipt": bool(l2_refs),
            "objects": l2_refs,
        }
        component_rfq = {"included": with_rfq}
        corrections_digest = ref.canonical_sha256(correction_refs)
        gap_digest = ref.canonical_sha256(gap_refs)
        l2_digest = ref.canonical_sha256(l2_refs)

    state = {
        "schema": ref.SCHEMA,
        "storage_mode": ref.STORAGE_MODE,
        "date": DATE,
        "source_seal_binding_sha256": seal_sha,
        "reference_set_sha256": reference_sha,
        "object_semantics_sha256": semantics_sha,
        "evidence_tier": "SEALED_CONFIRMATION",
        "evidence_basis_sha256": ref.canonical_sha256(evidence_basis),
        "corrections_digest": corrections_digest,
        "gap_evidence_digest": gap_digest,
        "l2_quality_digest": l2_digest,
        "tl1_status": "TL1",
        "rfq_policy": ref.RFQ_POLICY,
        "rfq_included": with_rfq,
        "canonical_receipt_set_sha256": receipt_set_sha,
        "canonical_receipt_binding_sha256":
            ref.canonical_sha256(receipt_binding),
    }
    if not legacy:
        state["manifest_contract_version"] = ref.MANIFEST_CONTRACT_VERSION
    state_sha = ref.canonical_sha256(state)
    rid = "%s__v3ref__seal-%s__pub-%s" % (
        DATE, seal_sha[:8], state_sha[:16])
    publication_components = {
        "seal_sha256": seal_sha,
        "corrections": component_corrections,
        "gap_evidence": component_gap,
        "rfq": component_rfq,
    }
    if not legacy:
        publication_components.update({
            "source": "IMMUTABLE_CANONICAL_RECEIPT_EXACT_VERSIONS",
            "l2_quality": component_l2,
        })
    channels["orderbooks_l2"]["seq_quality"] = component_l2
    manifest = {
        "schema": ref.SCHEMA,
        "schema_version": 3,
        "storage_mode": ref.STORAGE_MODE,
        "release_id": rid,
        "date": DATE,
        "publication_status": "PUBLISHED",
        "rfq_policy": ref.RFQ_POLICY,
        "rfq_included": with_rfq,
        "published_at_utc": "2026-07-14T04:00:00Z",
        "publisher_commit": "f" * 40,
        "canonical_receipt": {
            "schema_version": "canonical-object-receipt-v1",
            "state": "DURABLE_RECEIPT_VERIFIED",
            "authority": "CANONICAL_CONTROL_PLANE",
            "authoritative": True,
            "s3_published": True,
            "prune_eligible": False,
            "receipt_set_sha256": receipt_set_sha,
            "receipt_object": receipt_binding,
        },
        "source_seal": {
            "bucket": BUCKET,
            "key": seal_item["source_key"],
            "version_id": seal_item["source_version_id"],
            "size": seal_item["size"],
            "sha256": seal_item["sha256"],
            "verification_state": "PASS",
        },
        "reference_set_sha256": reference_sha,
        "object_semantics_sha256": semantics_sha,
        "publication_state": state,
        "publication_state_sha256": state_sha,
        "publication_components": publication_components,
        "l2_quality": component_l2,
        "evidence": {"tier": "SEALED_CONFIRMATION", "basis": evidence_basis},
        "evidence_tier": "SEALED_CONFIRMATION",
        "evidence_tier_basis": evidence_basis,
        "seal": {
            "sha256": seal_sha,
            "manifest_date_sha256": manifest_digest,
            "sealed_at": seal["sealed_at"],
            "status": "SEALED",
            "archive_files": len(archive),
            "archive_rows": len(archive),
            "capture_quality_status": "ASSESSED_PASS",
        },
        "tables": tables,
        "tl1_status": "TL1",
        "channels": channels,
        "corrections": {
            "included_files": len(correction_refs),
            "ledger_day_entries": 0 if legacy else None,
            "cutoff_utc": "2026-07-14T04:00:00Z",
            "digest": state["corrections_digest"],
            "note": "fixture",
        },
        "post_upload_verification": {
            "method": "authoritative canonical receipt exact-version binding",
            "objects_verified": len(objects),
            "data_objects_uploaded": 0,
        },
        "version_binding": {
            "mode": ref.STORAGE_MODE,
            "bindings_sha256": reference_sha,
            "versioning_requirement": "VERSIONING_REQUIRED",
        },
        "objects": objects,
    }
    if not legacy:
        manifest["manifest_contract_version"] = \
            ref.MANIFEST_CONTRACT_VERSION
        manifest["eligibility_enforcement"] = {
            "publisher_exact_tag_inspection": "NOT_AUTHORIZED_BY_DESIGN",
            "publisher_tag_mutation": "ACCESS_DENIED",
            "tagger_precommit_proof": {
                "schema_version":
                    "canonical-eligibility-precommit-proof-v1",
                "proof_sha256": ref.canonical_sha256({
                    "fixture": receipt_variant,
                    "receipt_set_sha256": receipt_set_sha,
                }),
                "generated_at_utc": "2026-07-14T03:59:59Z",
                "tagger_sts_caller_arn": (
                    "arn:aws:iam::123456789012:user/"
                    "canonical-eligibility-tagger"),
                "target_set_sha256": ref.canonical_sha256({
                    "fixture_targets": reference_sha,
                }),
                "target_count": len(objects) + 1,
                "research_candidate_count": len(objects),
                "rfq": "FRESH_SEALED" if with_rfq else "OFF",
            },
            "tagger_exact_set_readback":
                "CONTENT_ADDRESSED_PRECOMMIT_PROOF",
            "consumer_exact_tag_revalidation":
                "W09_S3_EXISTING_OBJECT_TAG_RESEARCH_ELIGIBLE_TRUE",
            "consumer_enforcement_scope":
                "EACH_CANONICAL_SOURCE_EXACT_VERSION",
        }
    if legacy and pin_legacy:
        _pin_legacy_manifest(manifest)
    return rid, manifest, source_bytes


def test_dated_ledger_reference_uses_forward_content_addressed_contract():
    digest = "a" * 64
    logical = "warehouse/corrections/date=%s/ledger_day.ndjson" % DATE
    item = {
        "logical_key": logical,
        "source_key": (
            "ec2/warehouse/publication-snapshots/v1/date=%s/corrections/"
            "ledger_day/sha256=%s/ledger_day.ndjson" % (DATE, digest)),
        "sha256": digest,
        "kind": "corrections_ledger_day",
        "channel": None,
        "date": DATE,
        "required": True,
    }
    local, special = ref._classify_object(item, DATE)
    assert local == "corrections/date=%s/ledger_day.ndjson" % DATE
    assert special is None
    item["logical_key"] = "warehouse/corrections/ledger_day.ndjson"
    with pytest.raises(ref.ReferenceManifestError):
        ref._classify_object(item, DATE)


def test_large_late_rows_reference_uses_original_canonical_exact_key():
    digest = "b" * 64
    logical = "warehouse/corrections/date=%s/late_rows.ndjson" % DATE
    item = {
        "logical_key": logical,
        "source_key": (
            "ec2/warehouse/corrections/date=%s/late_rows.ndjson" % DATE),
        "sha256": digest,
        "kind": "correction",
        "channel": None,
        "date": DATE,
        "required": True,
    }
    local, special = ref._classify_object(item, DATE)
    assert local == "corrections/date=%s/late_rows.ndjson" % DATE
    assert special is None

    item["source_key"] = (
        "ec2/warehouse/publication-snapshots/v1/date=%s/corrections/"
        "late_rows/sha256=%s/late_rows.ndjson" % (DATE, digest))
    with pytest.raises(ref.ReferenceManifestError):
        ref._classify_object(item, DATE)


class RecordingStore:
    def __init__(self, releases, objects, fail_key=None):
        self.releases = releases
        self.objects = objects
        self.fail_key = fail_key
        self.source_calls = []
        self.list_calls = []
        self.manifest_calls = []

    def list(self, prefix=""):
        self.list_calls.append(prefix)
        return [("releases/%s/MANIFEST.json" % rid, len(raw))
                for rid, raw in self.releases.items()]

    def get_bytes_with_version(self, rel):
        self.manifest_calls.append(rel)
        rid = rel.split("/")[1]
        return self.releases[rid], "manifest-version-%s" % rid[-16:]

    def get_source_to(self, bucket, key, dest, version_id):
        self.source_calls.append((bucket, key, version_id))
        if key == self.fail_key:
            raise OSError("fixture exact GET failure")
        payload = self.objects[(bucket, key, version_id)]
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(payload)

    def describe(self):
        return "fixture://research"


class EquivocatingManifestStore(RecordingStore):
    def get_bytes_with_version(self, rel):
        raw, version = super().get_bytes_with_version(rel)
        if len(self.manifest_calls) > 1:
            return raw + b" ", version + "-changed"
        return raw, version


def _store_for(*releases):
    manifests = {}
    objects = {}
    for rid, manifest, source in releases:
        manifests[rid] = _json_bytes(manifest)
        objects.update(source)
    return RecordingStore(manifests, objects)


def test_strict_manifest_contract_accepts_complete_core_release():
    rid, manifest, _source = build_release()
    descriptor = ref.validate_manifest(manifest, rid)
    assert descriptor["manifest_contract_version"] == \
        ref.MANIFEST_CONTRACT_VERSION
    assert descriptor["release_id"] == rid
    assert set(descriptor["tables"]) == {
        "orderbooks_l1", "orderbooks_full", "trades"}
    assert descriptor["storage_mode"] == "CANONICAL_REFERENCE"


def test_markerless_v3_requires_exact_historical_pin(monkeypatch):
    monkeypatch.setattr(ref, "LEGACY_V3_MANIFEST_PINS", frozenset())
    rid, manifest, _source = build_release(
        receipt_variant="unallowlisted-legacy", legacy=True,
        pin_legacy=False)
    with pytest.raises(ref.ReferenceManifestError,
                       match="explicit historical allowlist"):
        ref.validate_manifest(manifest, rid)

    pin = (rid, ref.canonical_sha256(manifest), manifest["published_at_utc"])
    monkeypatch.setattr(ref, "LEGACY_V3_MANIFEST_PINS", frozenset({pin}))
    assert ref.validate_manifest(manifest, rid)["release_id"] == rid

    changed = copy.deepcopy(manifest)
    changed["publisher_commit"] = "e" * 40
    with pytest.raises(ref.ReferenceManifestError,
                       match="explicit historical allowlist"):
        ref.validate_manifest(changed, rid)


def test_pinned_markerless_v3_cannot_cross_cutover(monkeypatch):
    monkeypatch.setattr(ref, "LEGACY_V3_MANIFEST_PINS", frozenset())
    rid, manifest, _source = build_release(
        receipt_variant="post-cutover-legacy", legacy=True,
        pin_legacy=False)
    manifest["published_at_utc"] = "2026-07-17T00:00:00Z"
    pin = (rid, ref.canonical_sha256(manifest), manifest["published_at_utc"])
    monkeypatch.setattr(ref, "LEGACY_V3_MANIFEST_PINS", frozenset({pin}))
    with pytest.raises(ref.ReferenceManifestError, match="fixed contract cutover"):
        ref.validate_manifest(manifest, rid)


def _bound_receipt(manifest, source):
    binding = manifest["canonical_receipt"]["receipt_object"]
    identity = (binding["bucket"], binding["key"], binding["version_id"])
    return json.loads(source[identity])


def _retarget_descriptor_receipt(descriptor, receipt):
    receipt["receipt_set_sha256"] = ref.canonical_sha256(
        ref.durable_receipt_projection(receipt))
    receipt["durability_set_sha256"] = ref._scoped_receipt_sha256(
        receipt["objects"], "durability_scope")
    receipt["research_candidate_set_sha256"] = ref._scoped_receipt_sha256(
        receipt["objects"], "research_candidate")
    descriptor["canonical_receipt_set_sha256"] = \
        receipt["receipt_set_sha256"]


def test_rfq_manifest_and_receipt_require_closed_dual_eligibility_contract():
    rid, manifest, source = build_release(with_rfq=True)
    descriptor = ref.validate_manifest(manifest, rid)
    receipt = _bound_receipt(manifest, source)
    result = ref.validate_durable_receipt(receipt, descriptor)
    rfq_rows = [item for item in receipt["objects"]
                if item["source_kind"] in {"raw_rfq", "raw_rfq_receipts"}]
    assert {item["source_kind"] for item in rfq_rows} == {
        "raw_rfq", "raw_rfq_receipts"}
    row = next(item for item in receipt["objects"]
               if item["source_kind"] == "raw_rfq")
    assert row["required"] is False
    assert row["eligibility_tag_state"] == "DUAL_TAGGED_VERIFIED"
    assert row["exposure_policy"] == "RESEARCH_ELIGIBLE_SEALED_RFQ"
    assert row["evidence_binding"]["rfq_exact_set_sha256"] == \
        ref._rfq_exact_set_sha256(rfq_rows)
    assert result["research_candidates"] == len(descriptor["objects"])


def test_rfq_manifest_accepts_later_receipt_day_named_by_seal():
    rid, manifest, source = build_release(
        with_rfq=True, rfq_date=LATE_RECEIPT_DATE)
    descriptor = ref.validate_manifest(manifest, rid)
    receipt = _bound_receipt(manifest, source)
    result = ref.validate_durable_receipt(receipt, descriptor)
    rfq_manifest = [item for item in descriptor["objects"]
                    if item["kind"] == "rfq"]
    rfq_receipt = [item for item in receipt["objects"]
                   if item["source_kind"] in {
                       "raw_rfq", "raw_rfq_receipts"}]
    assert {item["date"] for item in rfq_manifest} == {LATE_RECEIPT_DATE}
    assert {item["date"] for item in rfq_receipt} == {LATE_RECEIPT_DATE}
    assert result["research_candidates"] == len(descriptor["objects"])


def test_without_rfq_descriptor_still_ignores_eligible_receipt_rfq():
    rid, manifest, source = build_release(with_rfq=True)
    descriptor = ref.validate_manifest(manifest, rid)
    receipt = _bound_receipt(manifest, source)
    descriptor["rfq_included"] = False
    descriptor["objects"] = [
        item for item in descriptor["objects"] if item["kind"] != "rfq"]
    result = ref.validate_durable_receipt(receipt, descriptor)
    assert result["research_candidates"] == len(descriptor["objects"])


@pytest.mark.parametrize(("field", "bad_value"), [
    ("state", "PENDING"),
    ("evidence_tier", "SEALED_DEGRADED_EVIDENCE"),
    ("integrity_state", "DATA_INTEGRITY_BLOCKED"),
    ("quarantine_state", "QUARANTINED"),
    ("repair_branch_state", "OPEN"),
    ("eligibility_evidence_sha256", "A" * 64),
])
def test_manifest_rejects_nonclosed_or_malformed_rfq_binding(
        field, bad_value):
    rid, manifest, _source = build_release(with_rfq=True)
    rfq = next(item for item in manifest["objects"]
               if item["kind"] == "rfq")
    rfq["evidence_binding"][field] = bad_value
    _pin_legacy_manifest(manifest)
    with pytest.raises(ref.ReferenceManifestError,
                       match="RFQ eligibility binding|SHA-256"):
        ref.validate_manifest(manifest, rid)


@pytest.mark.parametrize(("field", "bad_value"), [
    ("required", True),
    ("eligibility_tag_state", "TAGGED_VERIFIED"),
    ("exposure_policy", "RESEARCH_ELIGIBLE"),
])
def test_receipt_rejects_rfq_without_optional_dual_tag_contract(
        field, bad_value):
    rid, manifest, source = build_release(with_rfq=True)
    descriptor = ref.validate_manifest(manifest, rid)
    receipt = _bound_receipt(manifest, source)
    row = next(item for item in receipt["objects"]
               if item["source_kind"] == "raw_rfq")
    row[field] = bad_value
    _retarget_descriptor_receipt(descriptor, receipt)
    with pytest.raises(ref.ReferenceManifestError):
        ref.validate_durable_receipt(receipt, descriptor)


def test_receipt_rejects_rfq_binding_that_does_not_bind_full_exact_set():
    rid, manifest, source = build_release(with_rfq=True)
    descriptor = ref.validate_manifest(manifest, rid)
    receipt = _bound_receipt(manifest, source)
    row = next(item for item in receipt["objects"]
               if item["source_kind"] == "raw_rfq")
    row["evidence_binding"]["rfq_exact_set_sha256"] = "0" * 64
    _retarget_descriptor_receipt(descriptor, receipt)
    with pytest.raises(ref.ReferenceManifestError,
                       match="exact set digest mismatch"):
        ref.validate_durable_receipt(receipt, descriptor)


@pytest.mark.parametrize("direction", ["receipt_extra", "manifest_extra"])
def test_manifest_and_seal_derived_receipt_rfq_sets_are_bidirectionally_equal(
        direction):
    rid, manifest, source = build_release(with_rfq=True)
    descriptor = ref.validate_manifest(manifest, rid)
    receipt = _bound_receipt(manifest, source)
    rfq = next(item for item in descriptor["objects"]
               if item["kind"] == "rfq")
    if direction == "receipt_extra":
        descriptor["objects"].remove(rfq)
    else:
        extra = copy.deepcopy(rfq)
        extra["logical_key"] = extra["logical_key"].replace(
            "rfq_23", "rfq_receipts_23")
        extra["source_key"] = extra["source_key"].replace(
            "rfq_23", "rfq_receipts_23")
        extra["source_version_id"] += "-extra"
        descriptor["objects"].append(extra)
    with pytest.raises(
            ref.ReferenceManifestError,
            match="manifest RFQ inventory does not equal"):
        ref.validate_durable_receipt(receipt, descriptor)


@pytest.mark.parametrize("mutation", [
    lambda m: m["objects"][0].update(source_bucket="other-bucket"),
    lambda m: m["objects"][0].update(source_key="ec2/warehouse/../escape"),
    lambda m: m["objects"][0].update(source_version_id="null"),
    lambda m: m["objects"][0].update(size=True),
    lambda m: m["objects"][0].update(sha256="A" * 64),
    lambda m: m["objects"][0].update(date="2026-07-12"),
    lambda m: m["objects"][0].update(channel="rfq"),
    lambda m: m["objects"][0].update(seal_binding="9" * 64),
    lambda m: m["objects"].append(copy.deepcopy(m["objects"][0])),
    lambda m: m["objects"].__setitem__(
        slice(None), [row for row in m["objects"]
                      if not row["logical_key"].endswith("markets.csv")]),
    lambda m: m["publication_state"].update(rfq_included=True),
    lambda m: m.update(release_id="2026-07-13__v3ref__seal-00000000__pub-"
                                  "0000000000000000"),
])
def test_manifest_negative_mutations_fail_closed(mutation):
    rid, manifest, _source = build_release()
    mutation(manifest)
    with pytest.raises(ref.ReferenceManifestError):
        ref.validate_manifest(manifest, rid)


def test_fetch_uses_only_exact_versions_and_builds_verified_view(tmp_path):
    rid, manifest, source = build_release()
    store = _store_for((rid, manifest, source))
    cache = str(tmp_path / "cache")
    assert rd.cmd_fetch(store, cache, rid, False) == 0
    assert len(store.source_calls) == 1 + len({row["sha256"]
                                               for row in manifest["objects"]})
    assert all(call in source for call in store.source_calls)
    assert store.list_calls == ["releases/"]
    marker = json.loads(Path(rd.verified_marker(cache, rid)).read_text())
    assert marker["storage_mode"] == "REFERENCE_V3"
    assert marker["version_binding_mode"] == "CANONICAL_REFERENCE"
    assert marker["rfq_status"] == "ABSENT_FROM_RELEASE"
    assert (Path(cache) / "view" / "facts" / "orderbooks_l1").exists()
    assert rd.cmd_verify(store, cache, rid) == 0


def test_rfq_is_not_downloaded_without_explicit_opt_in(tmp_path):
    rid, manifest, source = build_release(with_rfq=True)
    store = _store_for((rid, manifest, source))
    cache = str(tmp_path / "cache")
    rfq = next(row for row in manifest["objects"] if row["kind"] == "rfq")
    assert rd.cmd_fetch(store, cache, rid, False) == 0
    assert all(call[1] != rfq["source_key"] for call in store.source_calls)
    marker = json.loads(Path(rd.verified_marker(cache, rid)).read_text())
    assert marker["rfq_status"] == "NOT_FETCHED_OPT_IN"

    assert rd.cmd_fetch(store, cache, rid, True) == 0
    assert [call[1] for call in store.source_calls].count(rfq["source_key"]) == 1
    marker = json.loads(Path(rd.verified_marker(cache, rid)).read_text())
    assert marker["rfq_status"] == "VERIFIED_SEALED_RAW"


def test_corrupt_same_size_content_is_quarantined_and_refetched(tmp_path):
    rid, manifest, source = build_release()
    store = _store_for((rid, manifest, source))
    cache = str(tmp_path / "cache")
    assert rd.cmd_fetch(store, cache, rid, False) == 0
    target = manifest["objects"][0]
    content = Path(rd.reference_content_path(cache, target["sha256"]))
    content.write_bytes(b"x" * target["size"])
    before = len(store.source_calls)
    assert rd.cmd_fetch(store, cache, rid, False) == 0
    assert len(store.source_calls) == before + 1
    assert rd.sha256_file(str(content)) == target["sha256"]
    quarantined = list((Path(cache) / "quarantine" / "objects").rglob("*"))
    assert any(path.is_file() for path in quarantined)


def test_required_exact_get_failure_never_creates_release_tree(tmp_path):
    rid, manifest, source = build_release()
    failed = manifest["objects"][3]["source_key"]
    store = _store_for((rid, manifest, source))
    store.fail_key = failed
    cache = str(tmp_path / "cache")
    assert rd.cmd_fetch(store, cache, rid, False) == 2
    assert not Path(rd.cache_release_dir(cache, rid)).exists()
    assert (Path(cache) / "reference_failures" / (rid + ".json")).is_file()


def test_manifest_equivocation_during_fetch_never_materializes(tmp_path):
    rid, manifest, source = build_release()
    raw = _json_bytes(manifest)
    store = EquivocatingManifestStore({rid: raw}, source)
    cache = str(tmp_path / "cache")
    assert rd.cmd_fetch(store, cache, rid, False) == 2
    assert len(store.manifest_calls) == 2
    assert not Path(rd.cache_release_dir(cache, rid)).exists()


def test_tampered_durable_receipt_object_set_fails_closed(tmp_path):
    rid, manifest, source = build_release()
    binding = manifest["canonical_receipt"]["receipt_object"]
    old_identity = (binding["bucket"], binding["key"], binding["version_id"])
    receipt = json.loads(source.pop(old_identity))
    receipt["objects"].pop()
    tampered = _json_bytes(receipt)
    binding.update({
        "version_id": "receipt-version-tampered",
        "size": len(tampered),
        "sha256": _sha(tampered),
    })
    source[(binding["bucket"], binding["key"], binding["version_id"])] = tampered
    state = manifest["publication_state"]
    state["canonical_receipt_binding_sha256"] = ref.canonical_sha256(binding)
    state_sha = ref.canonical_sha256(state)
    manifest["publication_state_sha256"] = state_sha
    rid = "%s__v3ref__seal-%s__pub-%s" % (
        DATE, manifest["source_seal"]["sha256"][:8], state_sha[:16])
    manifest["release_id"] = rid
    _pin_legacy_manifest(manifest)

    store = _store_for((rid, manifest, source))
    cache = str(tmp_path / "cache")
    assert rd.cmd_fetch(store, cache, rid, False) == 2
    assert not Path(rd.cache_release_dir(cache, rid)).exists()
    failure = json.loads((Path(cache) / "reference_failures" /
                          (rid + ".json")).read_text())
    assert "receipt_set_sha256 does not recompute" in failure["failure"]


def test_content_cache_deduplicates_across_distinct_releases(tmp_path):
    first = build_release(receipt_variant="first")
    second = build_release(receipt_variant="second")
    assert first[0] != second[0]
    store = _store_for(first, second)
    cache = str(tmp_path / "cache")
    assert rd.cmd_fetch(store, cache, first[0], False) == 0
    first_calls = len(store.source_calls)
    assert rd.cmd_fetch(store, cache, second[0], False) == 0
    assert len(store.source_calls) == first_calls + 1
    content_files = [path for path in (Path(cache) / "objects" / "sha256").rglob("*")
                     if path.is_file()]
    assert len(content_files) == 2 + len({row["sha256"]
                                          for row in first[1]["objects"]})


def test_w09_selector_keeps_v2_default_and_requires_explicit_v3_opt_in(tmp_path):
    v2_rid = "2026-07-12__seal-aaaaaaaa__pub-bbbbbbbbbbbbbbbb"
    v2_dir = tmp_path / "releases" / v2_rid
    v2_dir.mkdir(parents=True)
    (v2_dir / "MANIFEST.json").write_text(json.dumps({
        "schema_version": "research-release-manifest-v2",
        "release_id": v2_rid,
        "date": "2026-07-12",
        "generated_at_utc": "2026-07-13T04:00:00Z",
        "version_binding": {"mode": "VERSION_BOUND"},
        "objects": [{"size": 1}],
    }))
    rid, manifest, _source = build_release()
    v3_dir = tmp_path / "releases" / rid
    v3_dir.mkdir()
    (v3_dir / "MANIFEST.json").write_bytes(_json_bytes(manifest))
    selector = ROOT / "deploy" / "w09" / "select_newest_release.py"
    default = subprocess.run(
        [sys.executable, str(selector), "--cache", str(tmp_path)],
        check=True, capture_output=True, text=True)
    assert default.stdout.strip() == v2_rid
    opted = subprocess.run(
        [sys.executable, str(selector), "--cache", str(tmp_path),
         "--include-v3-reference", "--json"],
        check=True, capture_output=True, text=True)
    result = json.loads(opted.stdout)
    assert result["release_id"] == rid
    assert result["storage_mode"] == "REFERENCE_V3"
