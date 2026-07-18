#!/usr/bin/env python3
"""W-PUB-REF-01B publisher contract: authoritative receipt -> v3 manifest.

All destinations are local fixtures.  No AWS, network, production checkout,
service, or W09 operation is used by this suite.
"""
import copy
import csv
import datetime
import glob
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import canonical_receipts as cr  # noqa: E402
import forward_canonical_receipts as fcr  # noqa: E402
import publication_generation as pg  # noqa: E402
import research_release as rr  # noqa: E402
import research_reference as ref  # noqa: E402
import test_research_bridge as bridge  # noqa: E402


DATE = "2026-07-11"
BUCKET = "fixture-canonical"
PREFIX = "ec2"


def _sha(payload):
    return hashlib.sha256(payload).hexdigest()


class FixtureReceiptReader:
    """Explicit exact-version object/tag reader for offline fixtures."""

    def __init__(self, binding, payload, objects=None,
                 parent_binding=None, parent_payload=None):
        self.binding = copy.deepcopy(binding)
        self.payload = payload
        self.parent_binding = copy.deepcopy(parent_binding)
        self.parent_payload = parent_payload
        self.ops = []
        self.objects = {}
        for obj in objects or []:
            self.objects[(obj["bucket"], obj["key"], obj["VersionId"])] = \
                copy.deepcopy(obj)
        self.head_overrides = {}
        self.tag_overrides = {}

    def _control(self, bucket, key, version_id):
        identity = (bucket, key, version_id)
        current = (self.binding["bucket"], self.binding["key"],
                   self.binding["VersionId"])
        if identity == current:
            return self.binding, self.payload
        if self.parent_binding is not None:
            parent = (self.parent_binding["bucket"],
                      self.parent_binding["key"],
                      self.parent_binding["VersionId"])
            if identity == parent:
                return self.parent_binding, self.parent_payload
        return None

    def head(self, bucket, key, version_id):
        self.ops.append(("head", bucket, key, version_id))
        identity = (bucket, key, version_id)
        control = self._control(bucket, key, version_id)
        if control is not None:
            obj, payload = control
            base = {
                "VersionId": version_id,
                "ContentLength": len(payload),
                "LastModified": obj["last_modified_utc"],
            }
        elif identity in self.objects:
            obj = self.objects[identity]
            base = {
                "VersionId": version_id,
                "ContentLength": obj["size"],
                "LastModified": obj["last_modified_utc"],
            }
        else:
            raise RuntimeError("unexpected exact version")
        base.update(self.head_overrides.get(identity, {}))
        return base

    def get_exact(self, bucket, key, version_id, dest):
        self.ops.append(("get_exact", bucket, key, version_id))
        control = self._control(bucket, key, version_id)
        if control is None:
            raise RuntimeError("unexpected control exact version")
        Path(dest).write_bytes(control[1])

    def get_tags(self, bucket, key, version_id):
        self.ops.append(("get_tags", bucket, key, version_id))
        identity = (bucket, key, version_id)
        control = self._control(bucket, key, version_id)
        if identity in self.tag_overrides:
            tags = self.tag_overrides[identity]
        elif control is not None and control[0] == self.binding:
            tags = {"research-eligible": "true"}
        elif identity in self.objects:
            obj = self.objects[identity]
            if obj.get("research_candidate") is not True:
                tags = {}
            elif rr._is_rfq_receipt_object(obj):
                tags = {"research-eligible": "true",
                        "research-channel": "rfq"}
            else:
                tags = {"research-eligible": "true"}
        else:
            raise RuntimeError("unexpected exact-version tag read")
        return {
            "VersionId": version_id,
            "TagSet": [{"Key": name, "Value": tags[name]}
                       for name in sorted(tags)],
        }


def _materialize_durable_index(root, receipt):
    tagged = copy.deepcopy(receipt)
    parent_evidence = {
        obj["logical_source_key"]: obj.get("_byte_parent_evidence_binding")
        for obj in tagged["objects"]
        if "_byte_parent_evidence_binding" in obj
    }
    eligibility_time = "2026-07-12T04:05:30Z"
    tagger_commit = "e" * 40
    for obj in tagged["objects"]:
        obj.pop("_byte_parent_evidence_binding", None)
        if obj.get("research_candidate") is True:
            obj["eligibility_verified_at_utc"] = eligibility_time
            obj["eligibility_tagger_code_commit"] = tagger_commit

    parent = copy.deepcopy(tagged)
    for field in (
            "receipt_phase", "byte_attestation_receipt_set_sha256",
            "eligibility_single_writer_audit_sha256",
            "eligibility_verified_at_utc", "eligibility_tagger_code_commit"):
        parent.pop(field, None)
    for obj in parent["objects"]:
        obj.pop("eligibility_verified_at_utc", None)
        obj.pop("eligibility_tagger_code_commit", None)
        if obj["logical_source_key"] in parent_evidence:
            obj["evidence_binding"] = parent_evidence[
                obj["logical_source_key"]]
            obj["research_candidate"] = False
            obj["required"] = False
            obj["exposure_policy"] = "FORBIDDEN_RFQ_DEFAULT"
        if obj.get("research_candidate") is True:
            obj["research_eligible"] = False
            obj["eligibility_tag_state"] = "SHADOW_NOT_TAGGED"
            obj["exposure_policy"] = "PENDING_ELIGIBILITY_TAG"
        elif obj["logical_source_key"] in parent_evidence:
            obj["research_eligible"] = False
            obj["eligibility_tag_state"] = "SHADOW_NOT_TAGGED"
    _refresh_receipt(parent)
    parent_payload = (json.dumps(parent, sort_keys=True, indent=2) +
                      "\n").encode()
    parent_sha = parent["receipt_set_sha256"]
    audit_sha = "2" * 64
    tagged.update({
        "receipt_phase": rr.CANONICAL_TAGGED_PHASE,
        "byte_attestation_receipt_set_sha256": parent_sha,
        "eligibility_single_writer_audit_sha256": audit_sha,
        "eligibility_verified_at_utc": eligibility_time,
        "eligibility_tagger_code_commit": tagger_commit,
    })
    payload = (json.dumps(tagged, sort_keys=True, indent=2) + "\n").encode()
    receipt_path = root / "canonical-receipt.json"
    receipt_path.write_bytes(payload)
    set_sha = tagged["receipt_set_sha256"]
    seal_key = tagged["seal"]["key"]
    seal_suffix = "warehouse/seals/date=%s.json" % tagged["date"]
    assert seal_key.endswith(seal_suffix)
    canonical_prefix = seal_key[:-len(seal_suffix)].rstrip("/")
    binding = {
        "bucket": receipt["seal"]["bucket"],
        "key": ("%s/control/canonical-receipts/v1/date=%s/"
                "receipt-%s.json") %
               (canonical_prefix, receipt["date"], set_sha),
        "VersionId": "receipt-version-%s" % _sha(payload)[:16],
        "size": len(payload),
        "sha256": _sha(payload),
        "last_modified_utc": "2026-07-12T04:06:00Z",
        "verification_state": "EXACT_VERSION_FULL_SHA256",
    }
    parent_binding = {
        "bucket": tagged["seal"]["bucket"],
        "key": ("%s/control/canonical-receipts/v1/date=%s/"
                "receipt-%s.json") %
               (canonical_prefix, tagged["date"], parent_sha),
        "VersionId": "byte-receipt-version-%s" % parent_sha[:16],
        "size": len(parent_payload),
        "sha256": _sha(parent_payload),
        "last_modified_utc": "2026-07-12T04:05:00Z",
        "verification_state": "EXACT_VERSION_FULL_SHA256",
    }
    index = {
        "schema_version": rr.CANONICAL_RECEIPT_INDEX_SCHEMA,
        "state": rr.CANONICAL_RECEIPT_STATE,
        "date": DATE,
        "receipt_set_sha256": set_sha,
        "receipt_object": binding,
        "receipt_payload_size": len(payload),
        "receipt_payload_sha256": _sha(payload),
        "complete": True,
        "completed": True,
        "prune_eligible": False,
        "receipt_phase": rr.CANONICAL_TAGGED_PHASE,
        "receipt_object_eligibility_tag_state": "TAGGED_VERIFIED",
        "byte_attestation_receipt_set_sha256": parent_sha,
        "byte_receipt_object": parent_binding,
        "eligibility_single_writer_audit_sha256": audit_sha,
    }
    index_path = root / "DURABLE-receipt-index.json"
    index_path.write_text(json.dumps(index, sort_keys=True, indent=2) + "\n")
    return (receipt_path, index_path, index, binding, payload,
            parent_binding, parent_payload, tagged)


def _payload_object(*, logical, key, payload, seal_sha, kind, family,
                    table=None, channel=None, candidate=True, required=True,
                    exposure="RESEARCH_ELIGIBLE",
                    evidence_binding="fixture-exact-binding"):
    return {
        "bucket": BUCKET,
        "key": key,
        "VersionId": "version-%s" % _sha(key.encode())[:16],
        "size": len(payload),
        "sha256": _sha(payload),
        "logical_source_key": logical,
        "source_kind": kind,
        "family": family,
        "table": table,
        "channel": channel,
        "date": DATE,
        "seal_binding": seal_sha,
        "evidence_binding": evidence_binding,
        "durability_verified": True,
        "research_eligible": bool(candidate),
        "eligibility_tag_state": ("TAGGED_VERIFIED" if candidate
                                  else "SHADOW_NOT_TAGGED"),
        "mutable_source": False,
        "required": required,
        "attestation_class": "DAY_SEAL_ATTESTED",
        "durability_scope": True,
        "research_candidate": candidate,
        "exposure_policy": exposure,
        "version_resolution": "EXACT_VERSION",
        "canonical_source": "FIXTURE_CANONICAL",
        "verification_state": "EXACT_VERSION_FULL_SHA256",
        "last_modified_utc": "2026-07-12T04:00:00Z",
    }


def _raw_kind(name):
    if name.startswith("rfq_receipts_"):
        return "raw_rfq_receipts", "rfq", "FORBIDDEN_RFQ_DEFAULT"
    if name.startswith("rfq_"):
        return "raw_rfq", "rfq", "FORBIDDEN_RFQ_DEFAULT"
    if name.startswith("l2_"):
        return "raw_l2", "orderbooks_full", "FORBIDDEN_RAW"
    return "raw_firehose", "firehose", "FORBIDDEN_RAW"


def _refresh_receipt(receipt):
    objects = receipt["objects"]
    by_family = {}
    for obj in objects:
        by_family.setdefault(obj["family"], []).append(obj)
    for family in receipt["families"]:
        rows = by_family.get(family["name"], [])
        family["observed_count"] = len(rows)
        if family["state"] != "NOT_APPLICABLE":
            family["expected_count"] = len(rows)
        projection = [{
            "bucket": row["bucket"], "key": row["key"],
            "size": row["size"], "sha256": row["sha256"],
        } for row in rows]
        projection.sort(key=lambda row: (row["bucket"], row["key"]))
        family["objects_digest"] = cr.canonical_sha256(projection)
    binding = dict(receipt["seal"])
    binding["families"] = receipt["families"]
    receipt["receipt_set_sha256"] = cr.receipt_set_sha256(
        receipt["date"], binding, objects)
    receipt["durability_set_sha256"] = cr.scoped_object_set_sha256(
        objects, "durability_scope")
    receipt["research_candidate_set_sha256"] = cr.scoped_object_set_sha256(
        objects, "research_candidate")
    return receipt


def _bind_eligible_rfq(receipt):
    candidates = [obj for obj in receipt["objects"]
                  if obj.get("research_candidate") is True
                  and rr._is_rfq_receipt_object(obj)]
    assert candidates
    exact_set_sha = rr._rfq_exact_set_sha256(candidates)
    binding = {
        "schema_version": rr.RFQ_ELIGIBILITY_BINDING_SCHEMA,
        "state": rr.RFQ_ELIGIBILITY_BINDING_STATE,
        "source": "seal.raw_files",
        "seal_sha256": receipt["seal"]["sha256"],
        "rfq_exact_set_sha256": exact_set_sha,
        "eligibility_evidence_sha256": "3" * 64,
        "evidence_tier": "SEALED_CONFIRMATION",
        "integrity_state": "PASS",
        "quarantine_state": "CLEAR",
        "repair_branch_state": "CLOSED_NO_REPAIR",
    }
    for obj in candidates:
        obj["eligibility_tag_state"] = rr.RFQ_DUAL_TAG_STATE
        obj["_byte_parent_evidence_binding"] = copy.deepcopy(
            obj.get("evidence_binding"))
        obj["evidence_binding"] = copy.deepcopy(binding)
    return _refresh_receipt(receipt)


def _make_receipt(root, quality, seal, seal_payload):
    warehouse = root / "warehouse"
    seal_sha = _sha(seal_payload)
    objects = []

    for proof in seal["archive_file_stats"]:
        payload = (warehouse / "facts" / proof["file"]).read_bytes()
        objects.append(_payload_object(
            logical="warehouse/facts/%s" % proof["file"],
            key="%s/warehouse/facts/%s" % (PREFIX, proof["file"]),
            payload=payload, seal_sha=seal_sha, kind="facts",
            family="facts", table=proof["table"], channel=proof["table"]))

    seal_obj = _payload_object(
        logical="warehouse/seals/date=%s.json" % DATE,
        key="%s/warehouse/seals/date=%s.json" % (PREFIX, DATE),
        payload=seal_payload, seal_sha=seal_sha, kind="seal", family="seal")
    objects.append(seal_obj)

    manifest_payload = (warehouse / "manifest.csv").read_bytes()
    day_manifest, manifest_digest, _rows = cr._manifest_day_csv(
        manifest_payload, DATE)
    assert manifest_digest == seal["manifest_date_sha256"]
    objects.append(_payload_object(
        logical="warehouse/manifest.csv",
        key=("%s/warehouse/publication-snapshots/v1/date=%s/manifest/"
             "sha256=%s/manifest.csv") % (PREFIX, DATE, _sha(day_manifest)),
        payload=day_manifest, seal_sha=seal_sha,
        kind="warehouse_manifest_day", family="manifest_date_projection"))

    dim_root = warehouse / "dim" / "snapshots" / ("date=%s" % DATE)
    for path in sorted(dim_root.iterdir()):
        payload = path.read_bytes()
        objects.append(_payload_object(
            logical="warehouse/dim/snapshots/date=%s/%s" % (DATE, path.name),
            key="%s/warehouse/dim/snapshots/date=%s/%s" %
                (PREFIX, DATE, path.name), payload=payload, seal_sha=None,
            kind="dim_snapshot", family="dim_snapshot",
            evidence_binding=None))

    catalog_root = warehouse / "catalog"
    for path in sorted(catalog_root.glob("*/part-00000.parquet")):
        rel = path.relative_to(catalog_root).as_posix()
        payload = path.read_bytes()
        objects.append(_payload_object(
            logical="warehouse/catalog/%s" % rel,
            key="%s/warehouse/catalog/%s" % (PREFIX, rel), payload=payload,
            seal_sha=seal_sha, kind="catalog",
            family="catalog_at_publication",
            table=rel.split("/", 1)[0]))

    gap_path = quality / ("capture_gap_receipt_%s.json" % DATE)
    gap_payload = gap_path.read_bytes()
    objects.append(_payload_object(
        logical="control/quality/v1/date=%s/capture_gap_receipt.json" % DATE,
        key=("%s/control/quality/v1/date=%s/capture_gap_receipt/"
             "sha256=%s/capture_gap_receipt.json") %
            (PREFIX, DATE, _sha(gap_payload)), payload=gap_payload,
        seal_sha=seal_sha,
        kind="capture_gap_receipt", family="capture_gap_receipt"))
    gap_projection = quality / "capture_gaps.csv"
    gap_projection_payload = gap_projection.read_bytes()
    objects.append(_payload_object(
        logical="control/quality/v1/date=%s/capture_gaps.csv" % DATE,
        key=("%s/control/quality/v1/date=%s/capture_gaps/"
             "sha256=%s/capture_gaps.csv") %
            (PREFIX, DATE, _sha(gap_projection_payload)),
        payload=gap_projection_payload, seal_sha=seal_sha,
        kind="capture_gaps_projection", family="capture_gaps_projection"))
    l2_path = quality / ("l2_gaps_%s.json" % DATE)
    l2_payload = l2_path.read_bytes()
    objects.append(_payload_object(
        logical="control/quality/v1/date=%s/l2_gaps.json" % DATE,
        key=("%s/control/quality/v1/date=%s/l2_gaps/"
             "sha256=%s/l2_gaps.json") %
            (PREFIX, DATE, _sha(l2_payload)), payload=l2_payload,
        seal_sha=seal_sha,
        kind="l2_quality_receipt", family="l2_quality"))

    for proof in seal["raw_files"]:
        payload = (root / "raw" / proof["file"]).read_bytes()
        kind, channel, exposure = _raw_kind(os.path.basename(proof["file"]))
        objects.append(_payload_object(
            logical="raw/%s" % proof["file"],
            key="%s/raw/%s" % (PREFIX, proof["file"]), payload=payload,
            seal_sha=seal_sha, kind=kind, family="raw_durability",
            channel=channel, candidate=False, exposure=exposure,
            required=(channel != "rfq"),
            evidence_binding="seal.raw_files"))

    family_specs = {
        "raw_durability": "REQUIRED_CORE",
        "facts": "REQUIRED_CORE",
        "seal": "REQUIRED_CORE",
        "manifest_date_projection": "REQUIRED_CORE",
        "dim_snapshot": "REQUIRED_RESEARCH",
        "catalog_at_publication": "REQUIRED_RESEARCH",
        "capture_gap_receipt": "REQUIRED_RESEARCH",
        "capture_gaps_projection": "CONDITIONAL",
        "l2_quality": "CONDITIONAL",
        "corrections_at_cutoff": "CONDITIONAL",
    }
    families = []
    for name, policy in sorted(family_specs.items()):
        count = sum(obj["family"] == name for obj in objects)
        families.append({
            "name": name,
            "policy": policy,
            "expected_basis": "fixture authoritative set",
            "expected_count": count,
            "observed_count": count,
            "state": ("PRESENT_VERIFIED" if count else "NOT_APPLICABLE"),
            "reason_code": "NONE",
            "semantic_sha256": cr.canonical_sha256([]),
            "objects_digest": None,
        })
    receipt = {
        "schema_version": rr.CANONICAL_RECEIPT_SCHEMA,
        "state": rr.CANONICAL_RECEIPT_STATE,
        "authority": rr.CANONICAL_RECEIPT_AUTHORITY,
        "authoritative": True,
        "s3_published": True,
        "prune_eligible": False,
        "date": DATE,
        "seal": {
            "date": DATE,
            "status": "SEALED",
            "version": seal["version"],
            "method": seal["method"],
            "bucket": seal_obj["bucket"],
            "key": seal_obj["key"],
            "VersionId": seal_obj["VersionId"],
            "size": seal_obj["size"],
            "sha256": seal_obj["sha256"],
            "manifest_date_sha256": seal["manifest_date_sha256"],
        },
        "families": families,
        "objects": objects,
        "verified_at_utc": "2026-07-12T04:05:00Z",
        "publisher_code_commit": "f" * 40,
    }
    return _refresh_receipt(receipt)


@pytest.fixture
def reference_tree(tmp_path, monkeypatch):
    root = tmp_path
    _l1, _tr, raw = bridge.make_day(
        str(root), DATE, tl1=True, with_rfq_raw=True, with_l2=True,
        go_eligible=True, capture_quality="ASSESSED_PASS")
    quality = root / "quality"
    quality.mkdir()
    day_us = bridge.wc.day_start_us(DATE)
    gap_rows = [{"start_us": day_us + 1_000,
                 "end_us": day_us + 90_000_000}]
    bridge.write_gap_receipt(str(quality), DATE, raw, gaps=gap_rows)
    (quality / "capture_gaps.csv").write_text(
        "start_us,end_us\n%d,%d\n" %
        (gap_rows[0]["start_us"], gap_rows[0]["end_us"]))
    bridge.write_l2_receipt(str(quality), DATE, raw)

    for name in ("series", "events", "markets", "settlements",
                 "series_classified"):
        path = root / "warehouse" / "catalog" / name / "part-00000.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("catalog-%s" % name).encode())
    dim = root / "warehouse" / "dim" / "snapshots" / ("date=%s" % DATE)
    dim.mkdir(parents=True)
    for name in ("series.csv", "events.csv", "markets.csv"):
        (dim / name).write_text("id,value\n%s,fixture\n" % name)

    # The older bridge fixture predates the canonical receipt contract and
    # writes machine-local absolute fact paths.  Canonical manifests are
    # portable, so freeze the same rows with workspace-relative paths and
    # update the synthetic seal's bound digest/count before constructing its
    # authoritative receipt.
    manifest_path = root / "warehouse" / "manifest.csv"
    with manifest_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames
        rows = list(reader)
    for row in rows:
        fact_path = Path(row["file_path"])
        row["file_md5"] = hashlib.md5(fact_path.read_bytes()).hexdigest()
        row["file_path"] = fact_path.relative_to(root).as_posix()
    with manifest_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    seal_path = root / "warehouse" / "seals" / ("date=%s.json" % DATE)
    seal = json.loads(seal_path.read_bytes())
    for proof in seal["archive_file_stats"]:
        proof["md5"] = hashlib.md5(
            (root / "warehouse" / "facts" / proof["file"]).read_bytes()
        ).hexdigest()
    seal["manifest_date_sha256"] = bridge.wc.manifest_date_sha256(
        str(manifest_path), DATE)[0]
    seal["archive_rows"] = sum(
        int(row["row_count"]) for row in rows if row["date"] == DATE)
    seal_path.write_text(json.dumps(seal, sort_keys=True, indent=1) + "\n")
    seal_payload = seal_path.read_bytes()
    seal = json.loads(seal_payload)
    receipt = _make_receipt(root, quality, seal, seal_payload)
    (receipt_path, index_path, index, receipt_binding, receipt_payload,
     parent_binding, parent_payload,
     tagged_receipt) = _materialize_durable_index(root, receipt)

    monkeypatch.setenv("WAREHOUSE_ROOT", str(root / "warehouse"))
    monkeypatch.setenv("ARCHIVE_ROOT", str(root / "warehouse" / "facts"))
    monkeypatch.setenv("RAW_ROOT", str(root / "raw"))
    monkeypatch.setenv("KALSHI_RESEARCH_ENV_FILE",
                       str(root / "no-research-key.env"))
    return {
        "root": root,
        "quality": quality,
        "receipt": receipt,
        "receipt_path": receipt_path,
        "index_path": index_path,
        "index": index,
        "receipt_binding": receipt_binding,
        "receipt_payload": receipt_payload,
        "parent_binding": parent_binding,
        "parent_payload": parent_payload,
        "tagged_receipt": tagged_receipt,
        "seal": seal,
    }


def _write_receipt(tree, receipt):
    material = _materialize_durable_index(tree["root"], receipt)
    keys = ("receipt_path", "index_path", "index", "receipt_binding",
            "receipt_payload", "parent_binding", "parent_payload",
            "tagged_receipt")
    tree.update(dict(zip(keys, material)))
    return str(tree["index_path"])


def _fixture_reader(tree, payload=None):
    return FixtureReceiptReader(
        tree["receipt_binding"],
        tree["receipt_payload"] if payload is None else payload,
        tree["tagged_receipt"]["objects"],
        tree["parent_binding"], tree["parent_payload"])


def _tag_precommit_proof(tree, *, mutate=None, generated_at=None):
    """Materialize the tagger/publisher handoff without publisher tag reads."""
    receipt = tree["tagged_receipt"]
    binding = tree["receipt_binding"]
    rows = [{
        "role": "TAGGED_RECEIPT",
        "logical_key": binding["key"],
        "source_bucket": binding["bucket"],
        "source_key": binding["key"],
        "source_version_id": binding["VersionId"],
        "size": binding["size"],
        "sha256": binding["sha256"],
        "required_tags": {"research-eligible": "true"},
    }]
    for obj in receipt["objects"]:
        if obj.get("research_candidate") is not True:
            continue
        is_rfq = rr._is_rfq_receipt_object(obj)
        rows.append({
            "role": "RESEARCH_CANDIDATE",
            "logical_key": obj["logical_source_key"],
            "source_bucket": obj["bucket"],
            "source_key": obj["key"],
            "source_version_id": obj["VersionId"],
            "size": obj["size"],
            "sha256": obj["sha256"],
            "required_tags": ({
                "research-eligible": "true", "research-channel": "rfq"}
                if is_rfq else {"research-eligible": "true"}),
        })
    rows.sort(key=lambda row: (
        row["role"], row["logical_key"], row["source_bucket"],
        row["source_key"], row["source_version_id"]))
    payload = {
        "schema_version": rr.TAG_PRECOMMIT_PROOF_SCHEMA,
        "state": rr.TAG_PRECOMMIT_PROOF_STATE,
        "date": DATE,
        "tagged_receipt_set_sha256": receipt["receipt_set_sha256"],
        "byte_attestation_receipt_set_sha256":
            receipt["byte_attestation_receipt_set_sha256"],
        "eligibility_single_writer_audit_sha256":
            receipt["eligibility_single_writer_audit_sha256"],
        "tagged_receipt_object": copy.deepcopy(binding),
        "target_set_sha256": rr.canonical_digest(rows),
        "target_count": len(rows),
        "research_candidate_count": len(rows) - 1,
        "targets": rows,
        "tagger_sts_caller_arn":
            "arn:aws:iam::123456789012:user/canonical-eligibility-tagger",
        "tagger_sts_account": "123456789012",
        "tagger_sts_user_id": "AIDAFIXTURETAGGER",
        "generated_at_utc": generated_at or datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rfq": (rr.RFQ_RESEARCH_MODE if any(
            rr._is_rfq_receipt_object(obj)
            and obj.get("research_candidate") is True
            for obj in receipt["objects"]) else "OFF"),
        "tag_puts": 0,
    }
    if mutate is not None:
        mutate(payload)
    raw = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
    path = tree["root"] / ("PRECOMMIT-%s.json" % _sha(raw))
    path.write_bytes(raw)
    return path


def _rewrite_tagged_control(tree, tagged):
    payload = (json.dumps(tagged, sort_keys=True, indent=2) + "\n").encode()
    binding = copy.deepcopy(tree["receipt_binding"])
    binding["size"] = len(payload)
    binding["sha256"] = _sha(payload)
    index = copy.deepcopy(tree["index"])
    index["receipt_object"] = binding
    index["receipt_payload_size"] = len(payload)
    index["receipt_payload_sha256"] = _sha(payload)
    tree["receipt_binding"] = binding
    tree["receipt_payload"] = payload
    tree["tagged_receipt"] = tagged
    tree["index"] = index
    tree["index_path"].write_text(
        json.dumps(index, sort_keys=True, indent=2) + "\n")
    return _fixture_reader(tree)


def _rewrite_parent_control(tree, parent):
    _refresh_receipt(parent)
    payload = (json.dumps(parent, sort_keys=True, indent=2) + "\n").encode()
    assert parent["receipt_set_sha256"] == \
        tree["index"]["byte_attestation_receipt_set_sha256"]
    binding = copy.deepcopy(tree["parent_binding"])
    binding["size"] = len(payload)
    binding["sha256"] = _sha(payload)
    index = copy.deepcopy(tree["index"])
    index["byte_receipt_object"] = binding
    tree["parent_binding"] = binding
    tree["parent_payload"] = payload
    tree["index"] = index
    tree["index_path"].write_text(
        json.dumps(index, sort_keys=True, indent=2) + "\n")
    return _fixture_reader(tree)


def _publish_reference(tree, dest, receipt=None, include_rfq=False,
                       receipt_reader=None, tag_reader=None,
                       yellow_alert=None, proof=None):
    if receipt is not None:
        _write_receipt(tree, receipt)
    reader = receipt_reader or _fixture_reader(tree)
    tree["last_receipt_reader"] = reader
    proof = proof or _tag_precommit_proof(tree)
    return rr.publish(
        DATE, str(dest), include_rfq, str(tree["quality"]),
        str(tree["root"] / "vault"), str(tree["root"] / "live"), False,
        reference_receipt=str(tree["index_path"]),
        reference_receipt_reader=reader,
        reference_tag_precommit_proof=str(proof),
        reference_yellow_alert=(str(yellow_alert)
                                if yellow_alert is not None else None))


def _publish_reference_input(tree, dest, index_path, reader):
    return rr.publish(
        DATE, str(dest), False, str(tree["quality"]),
        str(tree["root"] / "vault"), str(tree["root"] / "live"), False,
        reference_receipt=str(index_path),
        reference_receipt_reader=reader,
        reference_tag_precommit_proof=str(_tag_precommit_proof(tree)))


def _single_manifest(dest):
    paths = [Path(path) for path in glob.glob(
        str(dest / "releases" / "*" / "MANIFEST.json"))]
    assert len(paths) == 1
    return paths[0], json.loads(paths[0].read_text())


def test_reference_publish_is_manifest_only_and_digests_recompute(reference_tree):
    dest = reference_tree["root"] / "reference-dest"
    assert _publish_reference(reference_tree, dest) == 0
    path, manifest = _single_manifest(dest)
    all_files = [p for p in dest.rglob("*") if p.is_file()]
    assert all_files == [path]
    assert manifest["schema"] == rr.REFERENCE_MANIFEST_SCHEMA
    assert manifest["schema_version"] == 3
    assert manifest["manifest_contract_version"] == \
        rr.REFERENCE_CONTRACT_VERSION
    assert manifest["storage_mode"] == "CANONICAL_REFERENCE"
    assert manifest["post_upload_verification"]["data_objects_uploaded"] == 0
    assert manifest["rfq_included"] is False
    assert not any(obj["kind"] == "rfq" for obj in manifest["objects"])
    assert manifest["tables"]["orderbooks_l1"]
    assert manifest["tables"]["orderbooks_full"]
    assert manifest["channels"]["orderbooks_l2"]["status"] == \
        "INCLUDED_SEALED_FACTS"
    assert manifest["evidence"]["tier"] == "SEALED_CONFIRMATION"
    assert any(obj["logical_key"] ==
               "control/quality/v1/date=%s/capture_gaps.csv" % DATE
               and "/capture_gaps/sha256=" in obj["source_key"]
               for obj in manifest["objects"])
    assert manifest["canonical_receipt"]["authoritative"] is True
    assert manifest["canonical_receipt"]["s3_published"] is True
    assert manifest["canonical_receipt"]["prune_eligible"] is False
    receipt_object = manifest["canonical_receipt"]["receipt_object"]
    assert rr.canonical_digest(receipt_object) == \
        manifest["publication_state"][
            "canonical_receipt_binding_sha256"]
    operations = [op[0] for op in reference_tree["last_receipt_reader"].ops]
    assert operations[:4] == ["head", "get_exact", "head", "get_exact"]
    assert operations.count("get_exact") == 2
    assert operations.count("get_tags") == 0
    enforcement = manifest["eligibility_enforcement"]
    assert enforcement["publisher_exact_tag_inspection"] == \
        "NOT_AUTHORIZED_BY_DESIGN"
    assert enforcement["tagger_exact_set_readback"] == \
        "CONTENT_ADDRESSED_PRECOMMIT_PROOF"
    assert enforcement["consumer_exact_tag_revalidation"] == \
        "W09_S3_EXISTING_OBJECT_TAG_RESEARCH_ELIGIBLE_TRUE"
    assert enforcement["tagger_precommit_proof"]["target_count"] == \
        1 + len(manifest["objects"])
    components = manifest["publication_components"]
    state = manifest["publication_state"]
    assert state["manifest_contract_version"] == \
        rr.REFERENCE_CONTRACT_VERSION
    assert rr.canonical_digest(components["corrections"]["objects"]) == \
        state["corrections_digest"]
    assert rr.canonical_digest(components["gap_evidence"]["objects"]) == \
        state["gap_evidence_digest"]
    assert rr.canonical_digest(manifest["l2_quality"]["objects"]) == \
        state["l2_quality_digest"]

    six = ("logical_key", "source_bucket", "source_key",
           "source_version_id", "size", "sha256")
    assert manifest["objects"]
    assert all(all(obj.get(field) is not None for field in six)
               for obj in manifest["objects"])
    projection = [{field: obj[field] for field in six}
                  for obj in manifest["objects"]]
    assert rr.canonical_digest(projection) == manifest["reference_set_sha256"]
    semantics = [{field: obj.get(field) for field in (
        "logical_key", "kind", "channel", "date", "required",
        "seal_binding", "evidence_binding")}
        for obj in manifest["objects"]]
    assert rr.canonical_digest(semantics) == \
        manifest["object_semantics_sha256"]
    assert rr.canonical_digest(manifest["publication_state"]) == \
        manifest["publication_state_sha256"]
    assert manifest["release_id"] == (
        "%s__v3ref__seal-%s__pub-%s" %
        (DATE, manifest["seal"]["sha256"][:8],
         manifest["publication_state_sha256"][:16]))


def test_two_phase_prepare_then_fresh_proof_commit_skips_expensive_rebuild(
        reference_tree, monkeypatch, capsys):
    dest = reference_tree["root"] / "two-phase-dest"
    prepared_root = reference_tree["root"] / "prepared"
    stage_root = reference_tree["root"] / "commit-stage"
    stage_root.mkdir()
    monkeypatch.setenv("RESEARCH_STAGE_ROOT", str(stage_root))
    monkeypatch.setattr(ref, "TRUSTED_BUCKET",
                        reference_tree["receipt_binding"]["bucket"])
    reader = _fixture_reader(reference_tree)

    assert rr.publish(
        DATE, str(dest), False, str(reference_tree["quality"]),
        str(reference_tree["root"] / "vault"),
        str(reference_tree["root"] / "live"), False,
        reference_receipt=str(reference_tree["index_path"]),
        reference_receipt_reader=reader,
        reference_prepare_only=True,
        reference_prepare_output_root=str(prepared_root)) == 0
    plans = list(prepared_root.glob("PREPARED-*.json"))
    assert len(plans) == 1
    plan, plan_sha = rr._read_prepared_reference_plan(plans[0])
    assert plan["state"] == rr.REFERENCE_PREPARED_PLAN_STATE
    assert plan["manifest_template"]["published_at_utc"] is None
    assert plan["manifest_template"]["eligibility_enforcement"][
        "tagger_precommit_proof"] is None
    assert not list(dest.glob("releases/*/MANIFEST.json"))

    stale = _tag_precommit_proof(
        reference_tree, generated_at="2000-01-01T00:00:00Z")
    with pytest.raises(SystemExit, match="precommit proof is stale"):
        rr.publish(
            DATE, str(dest), False, str(reference_tree["quality"]),
            str(reference_tree["root"] / "vault"),
            str(reference_tree["root"] / "live"), False,
            reference_receipt=str(reference_tree["index_path"]),
            reference_receipt_reader=_fixture_reader(reference_tree),
            reference_tag_precommit_proof=str(stale),
            reference_prepared_plan=str(plans[0]))
    assert not list(dest.glob("releases/*/MANIFEST.json"))

    prepared_at = datetime.datetime.fromisoformat(
        plan["prepared_at_utc"].replace("Z", "+00:00"))
    before_prepare = _tag_precommit_proof(
        reference_tree,
        generated_at=(prepared_at - datetime.timedelta(seconds=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"))
    with pytest.raises(SystemExit, match="predates the prepared"):
        rr.publish(
            DATE, str(dest), False, str(reference_tree["quality"]),
            str(reference_tree["root"] / "vault"),
            str(reference_tree["root"] / "live"), False,
            reference_receipt=str(reference_tree["index_path"]),
            reference_receipt_reader=_fixture_reader(reference_tree),
            reference_tag_precommit_proof=str(before_prepare),
            reference_prepared_plan=str(plans[0]))

    fresh = _tag_precommit_proof(reference_tree)
    monkeypatch.setattr(
        rr, "verify_against",
        lambda *_a, **_kw: pytest.fail(
            "prepared commit reopened expensive fact inputs"))
    commit_reader = _fixture_reader(reference_tree)
    assert rr.publish(
        DATE, str(dest), False, str(reference_tree["quality"]),
        str(reference_tree["root"] / "vault"),
        str(reference_tree["root"] / "live"), False,
        reference_receipt=str(reference_tree["index_path"]),
        reference_receipt_reader=commit_reader,
        reference_tag_precommit_proof=str(fresh),
        reference_prepared_plan=str(plans[0])) == 0
    assert [op[0] for op in commit_reader.ops] == [
        "head", "get_exact", "head", "get_exact"]
    _path, manifest = _single_manifest(dest)
    assert manifest["eligibility_enforcement"]["tagger_precommit_proof"][
        "proof_sha256"] == fresh.name[10:-5]
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["state"] == "REFERENCE_MANIFEST_COMMITTED"
    assert result["prepared_plan_sha256"] == plan_sha
    assert result["manifest_object"]["sha256"] == _sha(
        _path.read_bytes())


def test_two_phase_prepared_plan_preserves_fresh_rfq_mode(
        reference_tree, monkeypatch, capsys):
    receipt = _eligible_rfq_receipt(reference_tree)
    _write_receipt(reference_tree, receipt)
    dest = reference_tree["root"] / "two-phase-rfq-dest"
    prepared_root = reference_tree["root"] / "prepared-rfq"
    stage_root = reference_tree["root"] / "commit-rfq-stage"
    stage_root.mkdir()
    monkeypatch.setenv("RESEARCH_STAGE_ROOT", str(stage_root))
    monkeypatch.setattr(ref, "TRUSTED_BUCKET",
                        reference_tree["receipt_binding"]["bucket"])

    assert rr.publish(
        DATE, str(dest), True, str(reference_tree["quality"]),
        str(reference_tree["root"] / "vault"),
        str(reference_tree["root"] / "live"), False,
        reference_receipt=str(reference_tree["index_path"]),
        reference_receipt_reader=_fixture_reader(reference_tree),
        reference_prepare_only=True,
        reference_prepare_output_root=str(prepared_root)) == 0
    plan_path = next(prepared_root.glob("PREPARED-*.json"))
    plan, _plan_sha = rr._read_prepared_reference_plan(plan_path)
    assert plan["include_rfq"] is True
    assert plan["manifest_template"]["rfq_included"] is True
    assert any(row["kind"] == "rfq"
               for row in plan["manifest_template"]["objects"])

    proof = _tag_precommit_proof(reference_tree)
    assert rr.publish(
        DATE, str(dest), True, str(reference_tree["quality"]),
        str(reference_tree["root"] / "vault"),
        str(reference_tree["root"] / "live"), False,
        reference_receipt=str(reference_tree["index_path"]),
        reference_receipt_reader=_fixture_reader(reference_tree),
        reference_tag_precommit_proof=str(proof),
        reference_prepared_plan=str(plan_path)) == 0
    _path, manifest = _single_manifest(dest)
    assert manifest["rfq_included"] is True
    assert manifest["eligibility_enforcement"]["tagger_precommit_proof"][
        "rfq"] == rr.RFQ_RESEARCH_MODE
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["rfq"] == rr.RFQ_RESEARCH_MODE


def test_prepared_plan_tamper_and_direct_real_s3_fail_closed(
        reference_tree, monkeypatch):
    prepared_root = reference_tree["root"] / "prepared-tamper"
    assert rr.publish(
        DATE, str(reference_tree["root"] / "unused-dest"), False,
        str(reference_tree["quality"]),
        str(reference_tree["root"] / "vault"),
        str(reference_tree["root"] / "live"), False,
        reference_receipt=str(reference_tree["index_path"]),
        reference_receipt_reader=_fixture_reader(reference_tree),
        reference_prepare_only=True,
        reference_prepare_output_root=str(prepared_root)) == 0
    plan = next(prepared_root.glob("PREPARED-*.json"))
    plan.write_bytes(plan.read_bytes() + b" ")
    with pytest.raises(SystemExit, match="content-address hash mismatch"):
        rr._read_prepared_reference_plan(plan)

    monkeypatch.setattr(rr, "require_reference_patrol_clear",
                        lambda *_a, **_kw: None)
    with pytest.raises(SystemExit, match="prepare-only -> tagger proof"):
        rr.publish(
            DATE, "s3://kalshi-vault-ritcardo/research", False,
            str(reference_tree["quality"]),
            str(reference_tree["root"] / "vault"),
            str(reference_tree["root"] / "live"), True,
            reference_receipt=str(reference_tree["index_path"]),
            reference_receipt_reader=_fixture_reader(reference_tree),
            reference_tag_precommit_proof=str(
                _tag_precommit_proof(reference_tree)))


@pytest.mark.parametrize("mutation,expected", [
    ("strip_marker", "unknown or mixed manifest contract version"),
    ("strip_enforcement", "eligibility_enforcement contract mismatch"),
    ("downgrade_marker", "unknown or mixed manifest contract version"),
    ("strip_both", "unknown or mixed manifest contract version"),
    ("legacy_components", "publication_components is incomplete"),
])
def test_manifest_contract_marker_fails_closed_on_stripping_or_mixed_downgrade(
        reference_tree, monkeypatch, mutation, expected):
    dest = reference_tree["root"] / ("contract-downgrade-" + mutation)
    assert _publish_reference(reference_tree, dest) == 0
    _path, manifest = _single_manifest(dest)
    monkeypatch.setattr(ref, "TRUSTED_BUCKET",
                        manifest["source_seal"]["bucket"])
    candidate = copy.deepcopy(manifest)
    if mutation == "strip_marker":
        candidate.pop("manifest_contract_version")
    elif mutation == "strip_enforcement":
        candidate.pop("eligibility_enforcement")
    elif mutation == "downgrade_marker":
        candidate["manifest_contract_version"] = "legacy"
    elif mutation == "strip_both":
        candidate.pop("manifest_contract_version")
        candidate.pop("eligibility_enforcement")
    else:
        components = candidate["publication_components"]
        candidate["publication_components"] = {
            key: components[key]
            for key in ("seal_sha256", "corrections", "gap_evidence", "rfq")
        }
    with pytest.raises(ref.ReferenceManifestError, match=expected):
        ref.validate_manifest(candidate, candidate["release_id"])


def test_actual_forward_inventory_receipt_publishes_reference(reference_tree):
    """Integration: consume forward_canonical_receipts, not a hand schema."""
    root = reference_tree["root"]
    production_bucket = "kalshi-vault-ritcardo"
    aux_path, descriptor = fcr.freeze_forward_auxiliary_set(
        DATE, production_bucket, PREFIX, str(root / "raw"),
        str(root / "warehouse"),
        str(reference_tree["quality"]), str(root / "forward-aux"))
    seal_path = root / "warehouse" / "seals" / ("date=%s.json" % DATE)
    _loaded_descriptor, auxiliary = fcr.load_forward_auxiliary_set(
        aux_path, DATE, production_bucket, PREFIX, reference_tree["seal"],
        _sha(seal_path.read_bytes()))
    observed_at = "2026-07-12T04:00:00Z"
    catalog_files = []
    for rel in cr.CATALOG_REQUIRED:
        source = root / "warehouse" / "catalog" / rel
        payload = source.read_bytes()
        catalog_files.append({
            "relative_path": "catalog/" + rel,
            "size": len(payload), "sha256": _sha(payload),
        })
    catalog_generation = pg.build_manifest("catalog", catalog_files)
    dim_files = []
    for name in cr.DIM_REQUIRED:
        source = (root / "warehouse" / "dim" / "snapshots"
                  / ("date=%s" % DATE) / name)
        payload = source.read_bytes()
        dim_files.append({
            "relative_path": "dim/snapshots/date=%s/%s" % (DATE, name),
            "size": len(payload), "sha256": _sha(payload),
        })
    dim_generation = pg.build_manifest(
        "dim", dim_files, date=DATE,
        source_catalog_generation_id=catalog_generation["generation_id"])
    witness_members = []
    for number, row in enumerate(
            catalog_generation["files"] + dim_generation["files"], 1):
        logical = "warehouse/" + row["relative_path"]
        witness_members.append({
            "logical_source_key": logical,
            "bucket": production_bucket,
            "key": PREFIX + "/" + logical,
            "VersionId": "generation-member-%d" % number,
            "size": row["size"], "sha256": row["sha256"],
            "LastModified": observed_at,
        })
    witness_members.sort(key=lambda row: row["logical_source_key"])
    seal_exact = {
        "bucket": production_bucket,
        "key": "%s/warehouse/seals/date=%s.json" % (PREFIX, DATE),
        "VersionId": "generation-seal-version",
        "size": len(seal_path.read_bytes()),
        "sha256": _sha(seal_path.read_bytes()),
        "LastModified": reference_tree["seal"]["sealed_at"],
    }
    witness = fcr.build_generation_witness_payload(
        DATE, production_bucket, PREFIX, seal_exact,
        catalog_generation, dim_generation, witness_members)
    witness_key, witness_raw = fcr.generation_witness_artifact(witness)
    witness_object = {
        "bucket": production_bucket, "key": witness_key,
        "VersionId": "generation-witness-version",
        "size": len(witness_raw), "sha256": _sha(witness_raw),
        "LastModified": observed_at,
    }
    resolutions = []
    for number, target in enumerate(
            fcr.forward_version_resolution_targets(auxiliary),
            len(witness_members) + 1):
        version_id = "forward-version-%d" % number
        resolutions.append({
            "logical_source_key": target["logical_source_key"],
            "bucket": target["bucket"], "key": target["key"],
            "size": target["size"], "sha256": target["sha256"],
            "VersionId": version_id, "LastModified": observed_at,
            "resolution": "CURRENT_EXACT_SHA256_MATCH",
            "resolver_evidence": {
                "logical_source_key": target["logical_source_key"],
                "key": target["key"], "current_version_id": version_id,
                "current_exact_sha256_match": True,
                "history_pages": 0, "versions_seen": 1,
                "same_size_candidates": 1, "exact_gets": 1,
                "matching_version_ids": [version_id],
                "selected_version_id": version_id,
                "selection_rule": fcr.FORWARD_VERSION_SELECTION_RULE,
            },
        })
    version_binding, _binding_payload = fcr.write_forward_version_binding(
        str(root / "forward-version-binding"), descriptor, auxiliary,
        DATE, production_bucket, PREFIX, witness, witness_object,
        resolutions)
    seal_binding, objects = fcr.build_forward_inventory(
        DATE, production_bucket, PREFIX, str(root / "raw"),
        str(root / "warehouse"),
        str(reference_tree["quality"]), aux_path, str(version_binding))
    total = len(objects)
    for obj in objects:
        obj.update({
            "VersionId": (obj.get("_expected_version_id")
                          or "version-%s" % _sha(obj["key"].encode())[:16]),
            "last_modified_utc": (
                obj.get("_expected_last_modified_utc") or observed_at),
            "durability_verified": True,
            "verification_state": "EXACT_VERSION_FULL_SHA256",
            "_inventory_complete": True,
            "_inventory_total": total,
        })
        if obj["research_candidate"]:
            obj["research_eligible"] = True
            obj["eligibility_tag_state"] = "TAGGED_VERIFIED"
            obj["exposure_policy"] = "RESEARCH_ELIGIBLE"
    shadow_path = cr.write_shadow_receipt(
        str(root / "forward-shadow"), DATE, seal_binding, objects,
        "2026-07-12T04:05:00Z", "f" * 40)
    durable = json.loads(Path(shadow_path).read_text())
    durable.update({
        "state": rr.CANONICAL_RECEIPT_STATE,
        "authority": rr.CANONICAL_RECEIPT_AUTHORITY,
        "authoritative": True,
        "s3_published": True,
        "prune_eligible": False,
    })
    dest = root / "actual-forward-reference"
    assert _publish_reference(reference_tree, dest, durable) == 0
    _path, manifest = _single_manifest(dest)
    projection = next(
        obj for obj in manifest["objects"]
        if obj["kind"] == "capture_gaps_projection")
    assert projection["logical_key"] == \
        "control/quality/v1/date=%s/capture_gaps.csv" % DATE
    assert "/capture_gaps/sha256=%s/" % projection["sha256"] in \
        projection["source_key"]
    assert ref.validate_manifest(manifest, manifest["release_id"])[
        "storage_mode"] == rr.REFERENCE_STORAGE_MODE


def test_legacy_publish_remains_copied_v2(reference_tree):
    dest = reference_tree["root"] / "copied-v2-dest"
    yellow = reference_tree["root"] / "active-reference-YELLOW.json"
    yellow.write_text('{"state":"YELLOW"}\n')
    rc = rr.publish(
        DATE, str(dest), False, str(reference_tree["quality"]),
        str(reference_tree["root"] / "vault"),
        str(reference_tree["root"] / "live"), False,
        reference_yellow_alert=str(yellow))
    assert rc == 0
    _path, manifest = _single_manifest(dest)
    assert manifest["schema_version"] == rr.MANIFEST_SCHEMA
    assert "storage_mode" not in manifest
    data_files = [p for p in dest.rglob("*")
                  if p.is_file() and p.name != "MANIFEST.json"]
    assert data_files


def test_active_yellow_alert_blocks_v3_at_publish_entry(reference_tree):
    dest = reference_tree["root"] / "yellow-blocked-reference"
    yellow = reference_tree["root"] / "active-reference-YELLOW.json"
    yellow.write_text('{"state":"YELLOW"}\n')
    with pytest.raises(SystemExit, match="reference patrol YELLOW"):
        _publish_reference(reference_tree, dest, yellow_alert=yellow)
    assert not dest.exists()

    fixed = Path(rr.reference_yellow_alert_path(
        str(reference_tree["root"] / "main-work-live")))
    fixed.parent.mkdir(parents=True)
    fixed.write_text('{"state":"YELLOW"}\n')
    with pytest.raises(SystemExit, match="reference patrol YELLOW"):
        rr.main([
            "research_release.py", "publish-reference", "--date", DATE,
            "--receipt", str(reference_tree["index_path"]),
            "--tag-precommit-proof",
            str(_tag_precommit_proof(reference_tree)),
            "--dest", str(dest), "--live-dir", str(fixed.parents[1]),
        ])


def test_real_s3_v3_cannot_override_live_dir_fixed_yellow(
        reference_tree, monkeypatch):
    live_dir = reference_tree["root"] / "main-live"
    fixed = Path(rr.reference_yellow_alert_path(str(live_dir)))
    fixed.parent.mkdir(parents=True)
    fixed.write_text('{"state":"YELLOW"}\n')
    alternate = reference_tree["root"] / "empty-alternate-YELLOW.json"

    def forbid_subprocess(*_args, **_kwargs):
        pytest.fail("fixed yellow gate must stop before any AWS/git subprocess")

    monkeypatch.setattr(rr.subprocess, "run", forbid_subprocess)
    with pytest.raises(SystemExit, match="cannot override"):
        rr.publish(
            DATE, "s3://kalshi-vault-ritcardo/research", False,
            str(reference_tree["quality"]),
            str(reference_tree["root"] / "vault"), str(live_dir), True,
            reference_receipt=str(reference_tree["index_path"]),
            reference_yellow_alert=str(alternate))


def test_s3_v3_manifest_rechecks_yellow_immediately_before_put(
        tmp_path, monkeypatch):
    live_dir = tmp_path / "main-live"
    fixed = Path(rr.reference_yellow_alert_path(str(live_dir)))
    manifest = tmp_path / "MANIFEST.json"
    manifest.write_text(json.dumps({
        "schema": rr.REFERENCE_MANIFEST_SCHEMA,
        "schema_version": 3,
        "storage_mode": rr.REFERENCE_STORAGE_MODE,
    }))
    dest = rr.S3Dest("s3://kalshi-vault-ritcardo/research")
    dest.bind_reference_patrol(str(live_dir))
    monkeypatch.setattr(rr, "require_clean_mutation_provenance",
                        lambda: "a" * 40)

    def history_then_yellow(_key):
        fixed.parent.mkdir(parents=True, exist_ok=True)
        fixed.write_text('{"state":"YELLOW"}\n')
        return None

    calls = []
    monkeypatch.setattr(dest, "_manifest_history", history_then_yellow)
    monkeypatch.setattr(
        rr.subprocess, "run",
        lambda args, **_kwargs: calls.append(args) or
        pytest.fail("yellow recheck must stop before put-object"))
    with pytest.raises(SystemExit, match="reference patrol YELLOW"):
        dest.upload_manifest(str(manifest), "releases/r/MANIFEST.json")
    assert calls == []


@pytest.mark.parametrize("fault", [
    "null_version", "receipt_digest", "extra", "path",
    "shadow_state", "untagged", "family_digest",
])
def test_reference_receipt_faults_fail_before_manifest(reference_tree, fault):
    receipt = copy.deepcopy(reference_tree["receipt"])
    candidates = [obj for obj in receipt["objects"]
                  if obj["research_candidate"]]
    fact = next(obj for obj in candidates if obj["source_kind"] == "facts")
    if fault == "null_version":
        fact["VersionId"] = "null"
        _refresh_receipt(receipt)
    elif fault == "receipt_digest":
        receipt["receipt_set_sha256"] = "0" * 64
    elif fault == "extra":
        extra = copy.deepcopy(fact)
        extra["logical_source_key"] = "warehouse/facts/extra.parquet"
        extra["key"] = "%s/warehouse/facts/extra.parquet" % PREFIX
        extra["VersionId"] = "extra-version"
        receipt["objects"].append(extra)
        _refresh_receipt(receipt)
    elif fault == "missing":
        receipt["objects"].remove(fact)
        _refresh_receipt(receipt)
    elif fault == "hash":
        fact["sha256"] = "a" * 64 if fact["sha256"] != "a" * 64 else "b" * 64
        _refresh_receipt(receipt)
    elif fault == "path":
        fact["key"] = "%s/warehouse/facts/../escape" % PREFIX
        _refresh_receipt(receipt)
    elif fault == "shadow_state":
        receipt["state"] = "RECEIPT_VERIFIED_SHADOW"
    elif fault == "untagged":
        fact["eligibility_tag_state"] = "SHADOW_NOT_TAGGED"
        fact["research_eligible"] = False
        _refresh_receipt(receipt)
    elif fault == "family_digest":
        next(family for family in receipt["families"]
             if family["name"] == "facts")["objects_digest"] = "0" * 64
        binding = dict(receipt["seal"])
        binding["families"] = receipt["families"]
        receipt["receipt_set_sha256"] = cr.receipt_set_sha256(
            receipt["date"], binding, receipt["objects"])
    dest = reference_tree["root"] / ("bad-%s" % fault)
    with pytest.raises(SystemExit):
        _publish_reference(reference_tree, dest, receipt)
    assert not list(dest.glob("releases/*/MANIFEST.json"))


@pytest.mark.parametrize("fault", [
    "direct_receipt", "null_index_version", "index_payload_sha",
    "exact_body_sha", "head_version",
])
def test_durable_index_and_exact_receipt_read_fail_closed(reference_tree,
                                                          fault):
    index_path = reference_tree["index_path"]
    reader = _fixture_reader(reference_tree)
    if fault == "direct_receipt":
        index_path = reference_tree["receipt_path"]
    elif fault == "null_index_version":
        index = copy.deepcopy(reference_tree["index"])
        index["receipt_object"]["VersionId"] = "null"
        index_path.write_text(json.dumps(index, sort_keys=True, indent=2) + "\n")
    elif fault == "index_payload_sha":
        index = copy.deepcopy(reference_tree["index"])
        index["receipt_payload_sha256"] = "0" * 64
        index_path.write_text(json.dumps(index, sort_keys=True, indent=2) + "\n")
    elif fault == "exact_body_sha":
        corrupted = bytearray(reference_tree["receipt_payload"])
        corrupted[len(corrupted) // 2] ^= 1
        reader = FixtureReceiptReader(
            reference_tree["receipt_binding"], bytes(corrupted),
            reference_tree["tagged_receipt"]["objects"],
            reference_tree["parent_binding"],
            reference_tree["parent_payload"])
    elif fault == "head_version":
        original_head = reader.head

        def wrong_head(bucket, key, version_id):
            result = original_head(bucket, key, version_id)
            result["VersionId"] = "different-version"
            return result

        reader.head = wrong_head
    dest = reference_tree["root"] / ("bad-durable-%s" % fault)
    with pytest.raises(SystemExit, match="durable (index|receipt)"):
        _publish_reference_input(reference_tree, dest, index_path, reader)
    assert not list(dest.glob("releases/*/MANIFEST.json"))


@pytest.mark.parametrize("kind", ["symlink", "oversized"])
def test_local_durable_index_is_nofollow_and_bounded(reference_tree, kind):
    unsafe = reference_tree["root"] / ("unsafe-index-%s.json" % kind)
    if kind == "symlink":
        unsafe.symlink_to(reference_tree["index_path"])
    else:
        unsafe.write_bytes(b"{" + b" " * rr.MAX_DURABLE_INDEX_BYTES + b"}")
    dest = reference_tree["root"] / ("unsafe-index-dest-%s" % kind)
    with pytest.raises(SystemExit, match="safely read|size cap"):
        _publish_reference_input(
            reference_tree, dest, unsafe, _fixture_reader(reference_tree))
    assert not list(dest.glob("releases/*/MANIFEST.json"))


@pytest.mark.parametrize("fault", [
    "pending_schema", "pending_state", "complete_false", "completed_false",
    "completed_missing", "pending_receipt_tag", "audit_lineage",
    "parent_lineage",
])
def test_final_index_fields_cannot_be_forged(reference_tree, fault):
    index = copy.deepcopy(reference_tree["index"])
    if fault == "pending_schema":
        index["schema_version"] = "canonical-tagged-receipt-pending-index-v1"
    elif fault == "pending_state":
        index["state"] = "TAGGED_RECEIPT_OBJECT_TAG_PENDING"
    elif fault == "complete_false":
        index["complete"] = False
    elif fault == "completed_false":
        index["completed"] = False
    elif fault == "completed_missing":
        index.pop("completed")
    elif fault == "pending_receipt_tag":
        index["receipt_object_eligibility_tag_state"] = \
            "PENDING_TAG_VERIFICATION"
    elif fault == "audit_lineage":
        index["eligibility_single_writer_audit_sha256"] = "4" * 64
    elif fault == "parent_lineage":
        index["byte_attestation_receipt_set_sha256"] = "5" * 64
    reference_tree["index_path"].write_text(
        json.dumps(index, sort_keys=True, indent=2) + "\n")
    dest = reference_tree["root"] / ("forged-final-%s" % fault)
    with pytest.raises(SystemExit):
        _publish_reference_input(
            reference_tree, dest, reference_tree["index_path"],
            _fixture_reader(reference_tree))
    assert not list(dest.glob("releases/*/MANIFEST.json"))


@pytest.mark.parametrize("fault", [
    "phase", "audit", "parent", "candidate_time", "candidate_commit",
])
def test_tagged_receipt_body_lineage_is_mandatory(reference_tree, fault):
    tagged = copy.deepcopy(reference_tree["tagged_receipt"])
    if fault == "phase":
        tagged["receipt_phase"] = "BYTE_ATTESTATION_ONLY"
    elif fault == "audit":
        tagged["eligibility_single_writer_audit_sha256"] = "6" * 64
    elif fault == "parent":
        tagged["byte_attestation_receipt_set_sha256"] = "7" * 64
    else:
        candidate = next(obj for obj in tagged["objects"]
                         if obj["research_candidate"])
        if fault == "candidate_time":
            candidate["eligibility_verified_at_utc"] = \
                "2026-07-12T04:05:31Z"
        else:
            candidate["eligibility_tagger_code_commit"] = "9" * 40
    reader = _rewrite_tagged_control(reference_tree, tagged)
    dest = reference_tree["root"] / ("forged-body-%s" % fault)
    with pytest.raises(SystemExit, match="tagged receipt|tag attestation"):
        _publish_reference_input(
            reference_tree, dest, reference_tree["index_path"], reader)
    assert not list(dest.glob("releases/*/MANIFEST.json"))


def test_byte_parent_semantics_cannot_change_behind_same_set_digest(
        reference_tree):
    parent = json.loads(reference_tree["parent_payload"])
    parent["objects"][0]["publisher_code_commit"] = "d" * 40
    reader = _rewrite_parent_control(reference_tree, parent)
    dest = reference_tree["root"] / "forged-parent-semantics"
    with pytest.raises(SystemExit, match="parent byte attestation"):
        _publish_reference_input(
            reference_tree, dest, reference_tree["index_path"], reader)


@pytest.mark.parametrize("fault", [
    "version", "size", "last_modified", "archive_class", "archive_status",
])
def test_candidate_exact_head_gate_fails_closed(reference_tree, fault):
    reader = _fixture_reader(reference_tree)
    candidate = next(obj for obj in reference_tree["tagged_receipt"]["objects"]
                     if obj["research_candidate"])
    identity = (candidate["bucket"], candidate["key"], candidate["VersionId"])
    override = {
        "version": {"VersionId": "different-version"},
        "size": {"ContentLength": candidate["size"] + 1},
        "last_modified": {"LastModified": "2026-07-12T04:00:01Z"},
        "archive_class": {"StorageClass": "DEEP_ARCHIVE"},
        "archive_status": {"ArchiveStatus": "ARCHIVE_ACCESS"},
    }[fault]
    reader.head_overrides[identity] = override
    dest = reference_tree["root"] / ("bad-head-%s" % fault)
    with pytest.raises(SystemExit, match="HEAD|storage class"):
        _publish_reference(reference_tree, dest, receipt_reader=reader)
    assert not list(dest.glob("releases/*/MANIFEST.json"))


@pytest.mark.parametrize("target", ["receipt", "candidate"])
def test_publisher_never_reads_tags_even_with_reader_tag_override(
        reference_tree, target):
    reader = _fixture_reader(reference_tree)
    if target == "receipt":
        obj = reference_tree["receipt_binding"]
    else:
        obj = next(item for item in reference_tree["tagged_receipt"]["objects"]
                   if item["research_candidate"])
    identity = (obj["bucket"], obj["key"], obj["VersionId"])
    reader.tag_overrides[identity] = {}
    dest = reference_tree["root"] / ("bad-tag-%s" % target)
    assert _publish_reference(reference_tree, dest, receipt_reader=reader) == 0
    assert all(op[0] != "get_tags" for op in reader.ops)


def test_precommit_proof_target_version_tamper_fails(reference_tree):
    def mutate(proof):
        proof["targets"][1]["source_version_id"] = "different-version"

    proof = _tag_precommit_proof(reference_tree, mutate=mutate)
    dest = reference_tree["root"] / "bad-tag-version"
    with pytest.raises(SystemExit, match="target-set digest mismatch"):
        _publish_reference(reference_tree, dest, proof=proof)


@pytest.mark.parametrize("fault", ["byte_tamper", "stale", "incomplete"])
def test_precommit_proof_tamper_stale_or_incomplete_fails_closed(
        reference_tree, fault):
    if fault == "stale":
        proof = _tag_precommit_proof(
            reference_tree, generated_at="2000-01-01T00:00:00Z")
        expected = "stale"
    elif fault == "incomplete":
        def mutate(payload):
            payload["targets"].pop()
            payload["target_count"] = len(payload["targets"])
            payload["research_candidate_count"] -= 1
            payload["target_set_sha256"] = rr.canonical_digest(
                payload["targets"])

        proof = _tag_precommit_proof(reference_tree, mutate=mutate)
        expected = "complete manifest reference set differ"
    else:
        proof = _tag_precommit_proof(reference_tree)
        proof.write_bytes(proof.read_bytes() + b" ")
        expected = "content-address hash mismatch"
    dest = reference_tree["root"] / ("bad-proof-" + fault)
    with pytest.raises(SystemExit, match=expected):
        _publish_reference(reference_tree, dest, proof=proof)
    assert not list(dest.glob("releases/*/MANIFEST.json"))


def test_offline_fixture_can_inject_separate_object_and_tag_readers(
        reference_tree):
    object_reader = _fixture_reader(reference_tree)
    tag_reader = _fixture_reader(reference_tree)
    dest = reference_tree["root"] / "separate-fixture-readers"
    assert _publish_reference(
        reference_tree, dest, receipt_reader=object_reader,
        tag_reader=tag_reader) == 0
    assert all(op[0] != "get_tags" for op in object_reader.ops)
    assert tag_reader.ops == []


def test_tagged_receipt_storage_class_must_be_online(reference_tree):
    reader = _fixture_reader(reference_tree)
    binding = reference_tree["receipt_binding"]
    identity = (binding["bucket"], binding["key"], binding["VersionId"])
    reader.head_overrides[identity] = {"StorageClass": "GLACIER"}
    dest = reference_tree["root"] / "archived-tagged-receipt"
    with pytest.raises(SystemExit, match="storage class"):
        _publish_reference(reference_tree, dest, receipt_reader=reader)


def test_forbidden_rfq_candidate_is_never_admitted(reference_tree):
    receipt = copy.deepcopy(reference_tree["receipt"])
    rfq = next(obj for obj in receipt["objects"]
               if obj["source_kind"] == "raw_rfq")
    rfq["research_candidate"] = True
    rfq["research_eligible"] = True
    rfq["eligibility_tag_state"] = rr.RFQ_DUAL_TAG_STATE
    # Deliberately retain FORBIDDEN_RFQ_DEFAULT from the durable receipt.
    _refresh_receipt(receipt)
    _bind_eligible_rfq(receipt)
    dest = reference_tree["root"] / "forbidden-rfq"
    with pytest.raises(SystemExit, match="forbidden"):
        _publish_reference(reference_tree, dest, receipt)
    assert not list(dest.glob("releases/*/MANIFEST.json"))


@pytest.mark.parametrize("missing_side", ["stage", "receipt"])
def test_canonical_receipt_not_mutable_stage_is_reference_authority(
        reference_tree, missing_side):
    receipt = copy.deepcopy(reference_tree["receipt"])
    if missing_side == "stage":
        (reference_tree["quality"] / "capture_gaps.csv").unlink()
    else:
        projection = next(obj for obj in receipt["objects"]
                          if obj["source_kind"] ==
                          "capture_gaps_projection")
        receipt["objects"].remove(projection)
        _refresh_receipt(receipt)
    dest = reference_tree["root"] / ("missing-projection-%s" % missing_side)
    assert _publish_reference(reference_tree, dest, receipt) == 0
    _path, manifest = _single_manifest(dest)
    projection_present = any(
        obj["kind"] == "capture_gaps_projection"
        for obj in manifest["objects"])
    assert projection_present is (missing_side == "stage")


def _eligible_rfq_receipt(tree):
    receipt = copy.deepcopy(tree["receipt"])
    rfq_objects = [obj for obj in receipt["objects"]
                   if obj["source_kind"] in
                   ("raw_rfq", "raw_rfq_receipts")]
    assert rfq_objects
    for obj in rfq_objects:
        obj.update({
            "research_candidate": True,
            "research_eligible": True,
            "eligibility_tag_state": rr.RFQ_DUAL_TAG_STATE,
            "exposure_policy": "RESEARCH_ELIGIBLE_SEALED_RFQ",
            "required": False,
        })
    _refresh_receipt(receipt)
    _bind_eligible_rfq(receipt)
    return receipt


@pytest.mark.parametrize("fault", [
    "schema", "state", "source", "seal", "set_digest", "evidence_digest",
    "tier", "integrity", "quarantine", "repair", "tag_state",
])
def test_rfq_structured_eligibility_binding_is_strict(reference_tree, fault):
    receipt = _eligible_rfq_receipt(reference_tree)
    rfq = next(obj for obj in receipt["objects"]
               if obj["source_kind"] == "raw_rfq")
    field_values = {
        "schema": ("schema_version", "wrong-schema"),
        "state": ("state", "PENDING"),
        "source": ("source", "operator-claim"),
        "seal": ("seal_sha256", "0" * 64),
        "set_digest": ("rfq_exact_set_sha256", "1" * 64),
        "evidence_digest": ("eligibility_evidence_sha256", "bad"),
        "tier": ("evidence_tier", "SEALED_DEGRADED_EVIDENCE"),
        "integrity": ("integrity_state", "DATA_INTEGRITY_BLOCKED"),
        "quarantine": ("quarantine_state", "QUARANTINED"),
        "repair": ("repair_branch_state", "REPAIR_OPEN"),
    }
    if fault == "tag_state":
        rfq["eligibility_tag_state"] = "TAGGED_VERIFIED"
    else:
        field, value = field_values[fault]
        rfq["evidence_binding"][field] = value
    _refresh_receipt(receipt)
    dest = reference_tree["root"] / ("bad-rfq-binding-%s" % fault)
    with pytest.raises(SystemExit, match="RFQ|DUAL_TAGGED_VERIFIED"):
        _publish_reference(reference_tree, dest, receipt, include_rfq=True)
    assert not list(dest.glob("releases/*/MANIFEST.json"))


def test_v3_precommit_path_includes_only_dual_tagged_sealed_rfq(reference_tree):
    receipt = _eligible_rfq_receipt(reference_tree)
    _write_receipt(reference_tree, receipt)
    dest = reference_tree["root"] / "rfq-precommit-on"
    assert _publish_reference(reference_tree, dest, include_rfq=True) == 0
    _path, manifest = _single_manifest(dest)
    rfq = [row for row in manifest["objects"] if row["kind"] == "rfq"]
    assert rfq
    assert manifest["rfq_included"] is True
    assert manifest["eligibility_enforcement"]["tagger_precommit_proof"][
        "rfq"] == rr.RFQ_RESEARCH_MODE


def test_dual_tagged_rfq_receipt_cannot_be_reinterpreted_as_rfq_off(
        reference_tree):
    receipt = _eligible_rfq_receipt(reference_tree)
    dest = reference_tree["root"] / "eligible-rfq"
    with pytest.raises(SystemExit, match="precommit proof.*binding"):
        _publish_reference(
            reference_tree, dest, receipt, include_rfq=False)
    assert not list(dest.glob("releases/*/MANIFEST.json"))


def test_manifest_conditional_create_conflict_fails_closed(reference_tree,
                                                           monkeypatch):
    dest = reference_tree["root"] / "conditional-conflict"
    assert _publish_reference(reference_tree, dest) == 0
    # Simulate a concurrent create after the exact-key absence check.
    monkeypatch.setattr(
        rr.LocalDest, "read_manifest", lambda _self, _key: None)
    with pytest.raises(SystemExit, match="write-once|already exists"):
        _publish_reference(reference_tree, dest)
    assert len(list(dest.glob("releases/*/MANIFEST.json"))) == 1


@pytest.mark.parametrize("history", [
    {
        "Versions": [{"Key": "research/releases/r/MANIFEST.json",
                      "VersionId": "old", "IsLatest": False}],
        "DeleteMarkers": [{"Key": "research/releases/r/MANIFEST.json",
                           "VersionId": "deleted", "IsLatest": True}],
    },
    {
        "Versions": [
            {"Key": "research/releases/r/MANIFEST.json",
             "VersionId": "one", "IsLatest": False},
            {"Key": "research/releases/r/MANIFEST.json",
             "VersionId": "two", "IsLatest": True},
        ],
        "DeleteMarkers": [],
    },
])
def test_s3_manifest_history_rejects_delete_markers_and_second_versions(
        monkeypatch, history):
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        assert args[1:3] == ["s3api", "list-object-versions"]
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps(history), stderr="")

    monkeypatch.setattr(rr.subprocess, "run", fake_run)
    dest = rr.S3Dest("s3://kalshi-vault-ritcardo/research")
    with pytest.raises(SystemExit, match="write-once history violated"):
        dest.read_manifest("releases/r/MANIFEST.json")
    assert len(calls) == 1


def test_s3_manifest_history_absence_never_falls_back_to_prefix_ls(
        monkeypatch):
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, stdout='{"Versions":[],"DeleteMarkers":[]}',
            stderr="")

    monkeypatch.setattr(rr.subprocess, "run", fake_run)
    dest = rr.S3Dest("s3://kalshi-vault-ritcardo/research")
    assert dest.read_manifest("releases/r/MANIFEST.json") is None
    assert len(calls) == 1
    assert calls[0][1:3] == ["s3api", "list-object-versions"]


def test_existing_reference_manifest_is_exactly_verified_before_noop(
        reference_tree):
    dest = reference_tree["root"] / "verified-noop"
    assert _publish_reference(reference_tree, dest) == 0
    path, _manifest = _single_manifest(dest)
    before = path.read_bytes()
    assert _publish_reference(reference_tree, dest) == 0
    assert path.read_bytes() == before
    assert len(list(dest.glob("releases/*/MANIFEST.json"))) == 1


def test_conflicting_existing_reference_manifest_never_noops(reference_tree):
    dest = reference_tree["root"] / "conflicting-noop"
    assert _publish_reference(reference_tree, dest) == 0
    path, manifest = _single_manifest(dest)
    manifest["objects"][0]["size"] += 1
    path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    with pytest.raises(SystemExit, match="existing reference manifest conflicts"):
        _publish_reference(reference_tree, dest)


def test_publish_reference_cli_is_explicit_and_defaults_rfq_off(
        reference_tree, monkeypatch):
    dest = reference_tree["root"] / "cli-reference"
    reader = _fixture_reader(reference_tree)
    monkeypatch.setattr(rr, "_make_canonical_receipt_reader", lambda: reader)
    rc = rr.main([
        "research_release.py", "publish-reference", "--date", DATE,
        "--receipt", str(reference_tree["index_path"]),
        "--tag-precommit-proof", str(_tag_precommit_proof(reference_tree)),
        "--dest", str(dest), "--quality-dir", str(reference_tree["quality"]),
        "--raw-vault", str(reference_tree["root"] / "vault"),
        "--live-dir", str(reference_tree["root"] / "live"),
    ])
    assert rc == 0
    _path, manifest = _single_manifest(dest)
    assert manifest["storage_mode"] == "CANONICAL_REFERENCE"
    assert manifest["rfq_included"] is False
    assert [op[0] for op in reader.ops][:4] == \
        ["head", "get_exact", "head", "get_exact"]
    assert all(op[0] != "get_tags" for op in reader.ops)
