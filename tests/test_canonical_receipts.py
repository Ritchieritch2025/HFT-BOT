#!/usr/bin/env python3
"""Offline contract tests for W-PUB-REF-01A canonical receipt shadowing.

These tests deliberately expose a very small read-only S3 surface: ``head``,
exact-version ``get`` and catalog-only ``list_versions``.  Shadow receipt
construction must not tag, overwrite, copy, or delete an S3 object, and it
must not alter the existing raw-prune path.
"""
import copy
import csv
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import canonical_receipts as cr  # noqa: E402
import canonical_receipt_control as crc  # noqa: E402
import forward_canonical_receipts as fcr  # noqa: E402
import warehouse_common as wc  # noqa: E402


DATE = "2026-07-13"
NEXT_DATE = "2026-07-14"
BUCKET = "kalshi-vault-fixture"
PREFIX = "ec2"


def _sha(payload):
    return hashlib.sha256(payload).hexdigest()


def _write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    path.write_bytes(payload)
    return payload


def _seal_file_entry(rel, payload):
    return {
        "file": rel,
        "size": len(payload),
        "sha256": _sha(payload),
        "checkpoint": len(payload),
        "inode": 1,
        "mtime_ns": 1,
        "ctime_ns": 1,
    }


def _archive_entry(rel, table, payload):
    return {
        "file": rel,
        "table": table,
        "size": len(payload),
        "sha256": _sha(payload),
        "md5": hashlib.md5(payload).hexdigest(),  # noqa: S324 - fixture only
        "inode": 1,
        "mtime_ns": 1,
        "ctime_ns": 1,
    }


@pytest.fixture
def receipt_tree(tmp_path):
    raw_root = tmp_path / "raw"
    warehouse = tmp_path / "warehouse"
    facts = warehouse / "facts"
    quality = tmp_path / "event_packs"
    payloads = {}

    raw_defs = [
        (DATE, "firehose_23.ndjson", b'{"channel":"ticker"}\n'),
        (DATE, "l2_23.ndjson.1", b'{"channel":"orderbook_delta"}\n'),
        (DATE, "rfq_23.ndjson.2", b'{"channel":"rfq"}\n'),
        (DATE, "rfq_receipts_23.ndjson", b'{"channel":"rfq_receipt"}\n'),
        # Cross-day receipt-time files remain under their real S3 date.  They
        # must never be rewritten into DATE merely because DATE owns the seal.
        (NEXT_DATE, "firehose_00.ndjson", b'{"channel":"ticker","xday":1}\n'),
        (NEXT_DATE, "rfq_00.ndjson", b'{"channel":"rfq","xday":1}\n'),
    ]
    raw_entries = []
    for day, name, payload in raw_defs:
        rel = "date=%s/%s" % (day, name)
        _write(raw_root / rel, payload)
        raw_entries.append(_seal_file_entry(rel, payload))
        payloads["%s/raw/%s" % (PREFIX, rel)] = payload

    fact_defs = [
        ("orderbooks_l1", "parquet", b"fixture-l1-parquet"),
        ("orderbooks_full", "parquet", b"fixture-l2-parquet"),
        ("trades", "csv.gz", b"fixture-trades-csv-gzip"),
    ]
    archive_entries = []
    manifest_rows = []
    for table, ext, payload in fact_defs:
        name = wc.partition_file(table, "Sports", "Baseball", DATE, ext)
        rel = "%s/category=Sports/subcategory=Baseball/date=%s/%s" \
              % (table, DATE, name)
        path = facts / rel
        _write(path, payload)
        archive_entries.append(_archive_entry(rel, table, payload))
        payloads["%s/warehouse/facts/%s" % (PREFIX, rel)] = payload
        manifest_rows.append([
            DATE, table, "Sports", "Baseball", 1,
            "work/warehouse/facts/%s" % rel,
            hashlib.md5(payload).hexdigest(), "2026-07-14T02:00:00Z",  # noqa: S324
        ])

    manifest = warehouse / "manifest.csv"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "date", "table", "category", "subcategory", "row_count",
            "file_path", "file_md5", "created_ts",
        ])
        writer.writerows(manifest_rows)
    manifest_payload = manifest.read_bytes()
    manifest_sha, _ = wc.manifest_date_sha256(str(manifest), DATE)

    catalog_binding_objects = []
    for name in ("series", "events", "markets"):
        dim_payload = _write(
            warehouse / "dim" / "snapshots" / ("date=%s" % DATE)
            / ("%s.csv" % name),
            ("id,value\n%s,fixture\n" % name).encode(),
        )
        payloads[
            "%s/warehouse/dim/snapshots/date=%s/%s.csv"
            % (PREFIX, DATE, name)
        ] = dim_payload

        catalog_payload = _write(
            warehouse / "catalog" / name / "part-00000.parquet",
            ("fixture-catalog-%s" % name).encode(),
        )
        catalog_key = "%s/warehouse/catalog/%s/part-00000.parquet" % (
            PREFIX, name)
        payloads[catalog_key] = catalog_payload
        catalog_binding_objects.append({
            "bucket": BUCKET,
            "key": catalog_key,
            "logical_source_key":
                "warehouse/catalog/%s/part-00000.parquet" % name,
            "VersionId": "v1",
            "size": len(catalog_payload),
            "sha256": _sha(catalog_payload),
            "last_modified_utc": "2026-07-14T02:30:00Z",
        })

    correction_row = {
        "table": "trades",
        "row": [wc.day_start_us(DATE) + 1, "fixture"],
        "observed_at_utc": "2026-07-14T03:00:00Z",
        "source_file": "/fixture/raw/date=%s/firehose_23.ndjson" % DATE,
        "source_raw_rel": "date=%s/firehose_23.ndjson" % DATE,
        "source_start_offset": 0,
        "source_end_offset": 100,
    }
    correction_payload = _write(
        warehouse / "corrections" / ("date=%s" % DATE)
        / "late_rows.ndjson",
        json.dumps(correction_row, sort_keys=True) + "\n",
    )
    payloads[
        "%s/warehouse/corrections/date=%s/late_rows.ndjson" % (PREFIX, DATE)
    ] = correction_payload
    ledger_payload = _write(
        warehouse / "corrections" / "ledger.ndjson",
        json.dumps({
            "event": "LATE_FACT_DIVERTED_TO_CORRECTIONS",
            "exchange_date": DATE,
            "n_rows": 1,
            "observed_at_utc": "2026-07-14T03:00:00Z",
            "source_file":
                "/fixture/raw/date=%s/firehose_23.ndjson" % DATE,
            "source_raw_rel": "date=%s/firehose_23.ndjson" % DATE,
            "seal_untouched": True,
        }, sort_keys=True) + "\n"
        + json.dumps({
            "event": "LATE_FACT_DIVERTED_TO_CORRECTIONS",
            "exchange_date": "2026-07-12",
            "n_rows": 1,
        }, sort_keys=True) + "\n",
    )

    day_start = wc.day_start_us(DATE)
    _write(
        quality / "capture_gaps.csv",
        "start_us,end_us\n%d,%d\n" % (day_start + 100, day_start + 200),
    )

    firehose_rows = [
        {"file": row["file"], "bytes": row["size"]}
        for row in raw_entries
        if row["file"].startswith("date=%s/firehose_" % DATE)
    ]
    gap_receipt_payload = _write(
        quality / ("capture_gap_receipt_%s.json" % DATE),
        json.dumps({
            "schema_version": "capture-gap-scan-receipt-v1",
            "date": DATE,
            "files": firehose_rows,
            "n_files": len(firehose_rows),
            "total_bytes": sum(row["bytes"] for row in firehose_rows),
            "records": 1,
            "unparsed": 0,
            "unreadable": False,
            "gaps": [{"start_us": day_start + 100,
                      "end_us": day_start + 200}],
            "raw_root": str(raw_root),
            "generated_at_utc": "2026-07-14T03:00:00Z",
        }, sort_keys=True) + "\n",
    )
    payloads[
        "%s/control/quality/v1/date=%s/capture_gap_receipt.json" % (PREFIX, DATE)
    ] = gap_receipt_payload

    l2_rows = [
        {"file": row["file"], "size": row["size"]}
        for row in raw_entries
        if row["file"].startswith("date=%s/l2_" % DATE)
    ]
    l2_payload = _write(
        quality / ("l2_gaps_%s.json" % DATE),
        json.dumps({
            "schema_version": "l2-gap-receipt-v1",
            "date": DATE,
            "files": [Path(row["file"]).name for row in l2_rows],
            "file_inventory": [
                {"file": row["file"], "bytes": row["size"]}
                for row in l2_rows
            ],
            "no_l2_files": False,
            "lines": 1,
            "parse_errors": 0,
            "sids_total": 1,
            "sids_with_seq_gaps": 0,
            "seq_gap_events": 0,
            "seq_missed_total": 0,
            "seq_regressions": 0,
            "stream_restarts": 0,
            "recorder_markers": {},
            "markers_lost_frames": 0,
            "snapshot_re_anchors_total": 0,
            "per_market": {},
            "generated_at_utc": "2026-07-14T03:00:00Z",
        }, sort_keys=True) + "\n",
    )
    payloads[
        "%s/control/quality/v1/date=%s/l2_gaps.json" % (PREFIX, DATE)
    ] = l2_payload

    seal = {
        "version": 2,
        "method": "full_v2",
        "status": "SEALED",
        "date": DATE,
        "sealed_at": "2026-07-14T02:00:00Z",
        "code_commit": "a" * 40,
        "go_no_go_eligible": True,
        "unverified": [],
        "manifest_date_sha256": manifest_sha,
        "archive_files": len(archive_entries),
        "archive_rows": len(archive_entries),
        "archive_file_stats": archive_entries,
        "raw_files": raw_entries,
        "capture_quality_status": "ASSESSED_PASS",
        "raw_retention_requirement": "LOCAL_OR_VAULT_VERIFIED_RECEIPT",
        "receipt_cross_day_hours": 2,
        "discovery_completeness": {},
    }
    seal_path = warehouse / "seals" / ("date=%s.json" % DATE)
    _write(seal_path, json.dumps(seal, sort_keys=True, indent=2) + "\n")
    payloads["%s/warehouse/seals/date=%s.json" % (PREFIX, DATE)] = \
        seal_path.read_bytes()

    seal_sha = _sha(seal_path.read_bytes())
    publication_state = {"seal_sha256": seal_sha, "fixture": True}
    publication_state_sha = cr.canonical_sha256(publication_state)
    source_release_id = "%s__seal-%s__pub-%s" % (
        DATE, seal_sha[:8], publication_state_sha[:16])
    source_objects = [{
        "key": "catalog/%s" % row["logical_source_key"].split(
            "warehouse/catalog/", 1)[1],
        "size": row["size"],
        "sha256": row["sha256"],
        "version_id": "research-v1",
    } for row in catalog_binding_objects]
    source_manifest = {
        "schema_version": "research-release-manifest-v2",
        "release_id": source_release_id,
        "date": DATE,
        "generated_at_utc": "2026-07-14T03:00:00Z",
        "publication_state": publication_state,
        "publication_state_sha256": publication_state_sha,
        "seal": {"sha256": seal_sha},
        "corrections": {"cutoff_utc": "2026-07-14T03:00:00Z"},
        "version_binding": {
            "mode": "VERSION_BOUND",
            "bindings_sha256": cr.canonical_sha256({
                row["key"]: row["version_id"] for row in source_objects
            }),
        },
        "post_upload_verification": {
            "objects_verified": len(source_objects),
        },
        "objects": source_objects,
    }
    source_manifest_payload = (json.dumps(
        source_manifest, sort_keys=True, indent=2) + "\n").encode()
    source_manifest_path = warehouse.parent / "source-v2-MANIFEST.json"
    _write(source_manifest_path, source_manifest_payload)
    source_manifest_key = "research/releases/%s/MANIFEST.json" % \
        source_release_id
    payloads[source_manifest_key] = source_manifest_payload

    catalog_bindings = {
        "schema_version": cr.CATALOG_BINDINGS_SCHEMA,
        "date": DATE,
        "bucket": BUCKET,
        "prefix": PREFIX,
        "provenance": "VERIFIED_V2_MATCHED_CANONICAL_VERSION_HISTORY",
        "source_release": {
            "release_id": source_release_id,
            "bucket": BUCKET,
            "key": source_manifest_key,
            "VersionId": "v1",
            "size": len(source_manifest_payload),
            "sha256": _sha(source_manifest_payload),
            "last_modified_utc": "2026-07-14T03:01:00Z",
            "local_path": str(source_manifest_path),
        },
        "objects": catalog_binding_objects,
    }
    aux_path, aux_descriptor = cr.freeze_auxiliary_set(
        DATE, BUCKET, PREFIX, str(raw_root), str(warehouse), str(quality),
        str(tmp_path / "auxiliary"), catalog_bindings)
    for row in aux_descriptor["objects"]:
        if row["local_relpath"] is not None:
            payloads[row["key"]] = (
                Path(aux_path) / row["local_relpath"]
            ).read_bytes()

    return {
        "raw_root": raw_root,
        "warehouse": warehouse,
        "quality": quality,
        "seal_path": seal_path,
        "seal": seal,
        "payloads": payloads,
        "catalog_bindings": catalog_bindings,
        "aux_bundle": Path(aux_path),
        "aux_descriptor": aux_descriptor,
        "source_release_id": source_release_id,
        "source_manifest_key": source_manifest_key,
    }


class FakeExactVersionClient:
    """In-memory exact-version S3 double; intentionally no write methods."""

    def __init__(self, payloads, missing_version_for=None,
                 corrupt_exact_for=None, race_key=None, missing_key=None,
                 missing_last_modified_for=None):
        self.ops = []
        self.latest = {}
        self.versions = {}
        self.version_modified = {}
        self.missing_version_for = missing_version_for
        self.corrupt_exact_for = corrupt_exact_for
        self.race_key = race_key
        self.missing_key = missing_key
        self.missing_last_modified_for = missing_last_modified_for
        for key, payload in payloads.items():
            object_id = (BUCKET, key)
            self.latest[object_id] = "v1"
            self.versions[(BUCKET, key, "v1")] = payload
            modified = ("2026-07-14T03:01:00Z"
                        if key.endswith("/MANIFEST.json")
                        and key.startswith("research/releases/")
                        else "2026-07-14T02:30:00Z")
            self.version_modified[(BUCKET, key, "v1")] = modified

    def head(self, bucket, key, version_id=None):
        self.ops.append(("head", bucket, key, version_id))
        if key == self.missing_key:
            raise cr.ReceiptError("PENDING_CANONICAL",
                                  "fixture object is not uploaded")
        object_id = (bucket, key)
        version_id = version_id or self.latest[object_id]
        payload = self.versions[(bucket, key, version_id)]
        result = {
            "ContentLength": len(payload),
            "LastModified": self.version_modified[
                (bucket, key, version_id)],
        }
        if key == self.missing_last_modified_for:
            result.pop("LastModified")
        if key != self.missing_version_for:
            result["VersionId"] = version_id
        if key == self.race_key:
            # Simulate a writer replacing latest immediately after HEAD.  A
            # correct verifier still GETs the returned v1, never mutable latest.
            self.versions[(bucket, key, "v2")] = b"raced-latest-bytes"
            self.version_modified[(bucket, key, "v2")] = \
                "2026-07-14T04:00:00Z"
            self.latest[object_id] = "v2"
        return result

    def list_versions(self, bucket, key):
        self.ops.append(("list_versions", bucket, key))
        rows = []
        for (row_bucket, row_key, version_id), payload in self.versions.items():
            if row_bucket == bucket and row_key == key:
                rows.append({
                    "Key": key,
                    "VersionId": version_id,
                    "LastModified": self.version_modified[
                        (bucket, key, version_id)],
                    "Size": len(payload),
                    "IsLatest": self.latest[(bucket, key)] == version_id,
                })
        return {"IsTruncated": False, "Versions": rows,
                "DeleteMarkers": []}

    def get_exact(self, bucket, key, version_id, dest):
        self.ops.append(("get_exact", bucket, key, version_id))
        payload = self.versions[(bucket, key, version_id)]
        if key == self.corrupt_exact_for:
            payload = b"same-size-is-not-enough".ljust(len(payload), b"!")[:len(payload)]
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(payload)


def _build(tree):
    return cr.build_desired_inventory(
        DATE,
        BUCKET,
        PREFIX,
        str(tree["raw_root"]),
        str(tree["warehouse"]),
        str(tree["quality"]),
        str(tree["aux_bundle"]),
    )


def _freeze_again(tree, aux_root, *, raw_root=None, warehouse=None,
                  quality=None, bindings=None):
    return cr.freeze_auxiliary_set(
        DATE, BUCKET, PREFIX,
        str(raw_root or tree["raw_root"]),
        str(warehouse or tree["warehouse"]),
        str(quality or tree["quality"]),
        str(aux_root),
        bindings or tree["catalog_bindings"],
    )


def _verify_all(tree, objects, **kwargs):
    client = FakeExactVersionClient(tree["payloads"], **kwargs)
    verified, failures, complete = cr.verify_inventory(
        objects, client, str(tree["warehouse"].parent / "verify_tmp")
    )
    return client, verified, failures, complete


def test_canonical_sha256_is_order_independent_and_content_sensitive():
    a = {"b": [2, {"y": "值", "x": 1}], "a": True}
    b = {"a": True, "b": [2, {"x": 1, "y": "值"}]}
    assert cr.canonical_sha256(a) == cr.canonical_sha256(b)
    b["b"][1]["x"] = 2
    assert cr.canonical_sha256(a) != cr.canonical_sha256(b)


@pytest.mark.parametrize("change", [
    {"status": "OPEN"},
    {"version": 1},
    {"method": "legacy_v0"},
    {"date": "2026-07-12"},
])
def test_load_verified_seal_rejects_non_authoritative_seal(receipt_tree, change):
    seal = dict(receipt_tree["seal"])
    seal.update(change)
    receipt_tree["seal_path"].write_text(json.dumps(seal))
    with pytest.raises(cr.ReceiptError):
        cr.load_verified_seal(str(receipt_tree["warehouse"]), DATE)


def test_inventory_maps_non_rfq_inputs_and_declares_rfq_blocked(receipt_tree):
    seal_binding, objects = _build(receipt_tree)
    assert seal_binding["date"] == DATE
    assert len(seal_binding["sha256"]) == 64

    required = {
        "bucket", "key", "logical_source_key", "source_kind", "table",
        "channel", "date", "size", "sha256", "seal_binding",
        "durability_verified", "research_eligible", "eligibility_tag_state",
    }
    assert objects
    assert all(required <= set(obj) for obj in objects)
    assert len({(obj["bucket"], obj["key"]) for obj in objects}) == len(objects)

    by_key = {obj["key"]: obj for obj in objects}
    expected = set(receipt_tree["payloads"])
    rfq_keys = {
        key for key in expected
        if "/raw/" in key and cr.RFQ_RE.match(os.path.basename(key))
    }
    assert rfq_keys
    assert set(by_key) == expected - rfq_keys
    assert not any(obj.get("channel") == "rfq" for obj in objects)
    families = {row["name"]: row for row in seal_binding["families"]}
    deferred = families["raw_rfq_deferred"]
    assert deferred["policy"] == "DATA_INTEGRITY_BLOCKED"
    assert deferred["state"] == "NOT_APPLICABLE"
    assert deferred["reason_code"] == "RFQ_BRANCH_CLOSED_NO_REPAIR"

    # Non-RFQ receipt-time bytes keep their real cross-day location.
    cross_firehose = "%s/raw/date=%s/firehose_00.ndjson" % (
        PREFIX, NEXT_DATE)
    assert by_key[cross_firehose]["date"] == NEXT_DATE
    assert by_key[cross_firehose]["channel"] == "firehose"

    fact_key = "%s/warehouse/facts/orderbooks_l1/category=Sports/" \
               "subcategory=Baseball/date=%s/" \
               "orderbooks_l1__Sports__Baseball__%s.parquet" \
               % (PREFIX, DATE, DATE)
    assert by_key[fact_key]["table"] == "orderbooks_l1"
    assert by_key[fact_key]["source_kind"] == "facts"

    for key in (
        "%s/warehouse/seals/date=%s.json" % (PREFIX, DATE),
        "%s/warehouse/dim/snapshots/date=%s/markets.csv" % (PREFIX, DATE),
        "%s/warehouse/catalog/markets/part-00000.parquet" % PREFIX,
        "%s/warehouse/corrections/date=%s/late_rows.ndjson" % (PREFIX, DATE),
        "%s/control/quality/v1/date=%s/capture_gap_receipt.json" % (PREFIX, DATE),
        "%s/control/quality/v1/date=%s/l2_gaps.json" % (PREFIX, DATE),
    ):
        assert key in by_key

    by_logical = {obj["logical_source_key"]: obj for obj in objects}
    manifest = by_logical["warehouse/manifest.csv"]
    assert "publication-snapshots/v1/date=%s/manifest/sha256=" % DATE \
        in manifest["key"]
    assert manifest["research_candidate"] is True
    ledger = by_logical["warehouse/corrections/ledger_day.ndjson"]
    assert "date=%s/corrections/ledger_day/sha256=" % DATE in ledger["key"]
    catalog = by_logical[
        "warehouse/catalog/markets/part-00000.parquet"
    ]
    assert catalog["_expected_version_id"] == "v1"
    assert catalog["version_resolution"] == "PRE_RESOLVED_HISTORICAL_EXACT"
    assert "%s/warehouse/manifest.csv" % PREFIX not in by_key
    assert "%s/warehouse/corrections/ledger.ndjson" % PREFIX not in by_key

    # Shadow is an attestation phase, not an eligibility/tagging phase.
    assert all(obj["durability_verified"] is False for obj in objects)
    assert all(obj["research_eligible"] is False for obj in objects)
    assert all("SHADOW" in obj["eligibility_tag_state"] for obj in objects)


def test_shadow_default_reader_never_touches_rfq_but_verifies_non_rfq(
        receipt_tree, tmp_path, monkeypatch):
    client = FakeExactVersionClient(receipt_tree["payloads"])
    monkeypatch.setattr(cr, "AwsCliS3Client", lambda _executable: client)
    monkeypatch.setattr(cr, "_code_commit", lambda: "a" * 40)
    output = tmp_path / "rfq-off-shadow"
    rc = cr.main([
        "shadow", "--date", DATE, "--bucket", BUCKET,
        "--prefix", PREFIX,
        "--raw-root", str(receipt_tree["raw_root"]),
        "--warehouse-root", str(receipt_tree["warehouse"]),
        "--quality-dir", str(receipt_tree["quality"]),
        "--aux-bundle", str(receipt_tree["aux_bundle"]),
        "--output-root", str(output),
    ])
    assert rc == 0
    read_keys = [op[2] for op in client.ops]
    assert read_keys
    assert not any(cr.RFQ_RE.match(os.path.basename(key)) for key in read_keys)
    assert "%s/raw/date=%s/firehose_23.ndjson" % (PREFIX, DATE) in read_keys
    assert "%s/raw/date=%s/l2_23.ndjson.1" % (PREFIX, DATE) in read_keys

    receipt = json.loads(next(output.glob("date=*/receipt-*.json")).read_text())
    families = {row["name"]: row for row in receipt["families"]}
    assert families["raw_rfq_deferred"]["policy"] == \
        "DATA_INTEGRITY_BLOCKED"
    assert families["raw_rfq_deferred"]["reason_code"] == \
        "RFQ_BRANCH_CLOSED_NO_REPAIR"


def test_plan_cli_reports_rfq_blocked_without_creating_an_s3_reader(
        receipt_tree, tmp_path, monkeypatch, capsys):
    def forbid_s3(_executable):
        pytest.fail("plan must not create an S3 reader")

    monkeypatch.setattr(cr, "AwsCliS3Client", forbid_s3)
    rc = cr.main([
        "plan", "--date", DATE, "--bucket", BUCKET,
        "--prefix", PREFIX,
        "--raw-root", str(receipt_tree["raw_root"]),
        "--warehouse-root", str(receipt_tree["warehouse"]),
        "--quality-dir", str(receipt_tree["quality"]),
        "--aux-bundle", str(receipt_tree["aux_bundle"]),
        "--output-root", str(tmp_path / "rfq-off-plan"),
    ])
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert "raw_rfq" not in result["kinds"]
    assert "raw_rfq_receipts" not in result["kinds"]
    families = {row["name"]: row for row in result["families"]}
    assert families["raw_rfq_deferred"] == {
        "name": "raw_rfq_deferred",
        "policy": "DATA_INTEGRITY_BLOCKED",
        "state": "NOT_APPLICABLE",
        "reason_code": "RFQ_BRANCH_CLOSED_NO_REPAIR",
        "observed_count": 0,
        "expected_count": 0,
    }


def test_legacy_inventory_rfq_capability_is_function_only(receipt_tree):
    _binding, objects = cr.build_desired_inventory(
        DATE, BUCKET, PREFIX, str(receipt_tree["raw_root"]),
        str(receipt_tree["warehouse"]), str(receipt_tree["quality"]),
        str(receipt_tree["aux_bundle"]), include_rfq_durability=True)
    rfq = [row for row in objects if row.get("channel") == "rfq"]
    assert rfq
    assert all(row["required"] is False
               and row["research_candidate"] is False
               and row["research_eligible"] is False
               and row["exposure_policy"] == "FORBIDDEN_RFQ_DEFAULT"
               for row in rfq)
    cli_source = inspect.getsource(cr.main)
    assert "include_rfq_durability" not in cli_source
    assert "--with-rfq" not in cli_source
    assert "--include-rfq" not in cli_source


def test_frozen_d_inventory_ignores_d_plus_1_control_mutations(receipt_tree):
    before_binding, before_objects = _build(receipt_tree)
    before_descriptor = (receipt_tree["aux_bundle"] / "AUX_SET.json").read_bytes()

    with (receipt_tree["warehouse"] / "manifest.csv").open(
            "a", newline="") as f:
        csv.writer(f).writerow([
            NEXT_DATE, "trades", "Sports", "Baseball", 1,
            "work/warehouse/facts/trades/date=%s/example.csv.gz" % NEXT_DATE,
            "0" * 32, "2026-07-15T02:00:00Z",
        ])
    (receipt_tree["warehouse"] / "catalog" / "markets"
     / "part-00000.parquet").write_bytes(b"d-plus-one-live-catalog")
    with (receipt_tree["warehouse"] / "corrections"
          / "ledger.ndjson").open("a") as f:
        f.write(json.dumps({
            "event": "LATE_FACT_DIVERTED_TO_CORRECTIONS",
            "exchange_date": NEXT_DATE,
            "n_rows": 99,
        }, sort_keys=True) + "\n")

    after_binding, after_objects = _build(receipt_tree)
    assert (receipt_tree["aux_bundle"] / "AUX_SET.json").read_bytes() == \
        before_descriptor
    assert [cr._public_object(row) for row in before_objects] == \
        [cr._public_object(row) for row in after_objects]
    assert before_binding["families"] == after_binding["families"]

    _client, before_verified, failures, complete = _verify_all(
        receipt_tree, before_objects)
    assert complete and not failures
    _client, after_verified, failures, complete = _verify_all(
        receipt_tree, after_objects)
    assert complete and not failures
    assert cr.receipt_set_sha256(
        DATE, before_binding, before_verified) == cr.receipt_set_sha256(
            DATE, after_binding, after_verified)


def test_aux_set_is_reproducible_relocatable_and_copies_no_catalog(
        receipt_tree, tmp_path):
    same_path, same = _freeze_again(receipt_tree, tmp_path / "same-aux")
    assert same["aux_set_sha256"] == \
        receipt_tree["aux_descriptor"]["aux_set_sha256"]
    assert Path(same_path).name == receipt_tree["aux_bundle"].name
    assert all(row["local_relpath"] is None
               for row in same["objects"]
               if row["family"] == "catalog_at_cutoff")
    assert not list(Path(same_path).glob("catalog/**/*"))

    relocated = tmp_path / "relocated"
    raw = relocated / "raw"
    warehouse = relocated / "warehouse"
    quality = relocated / "event_packs"
    shutil.copytree(receipt_tree["raw_root"], raw)
    shutil.copytree(receipt_tree["warehouse"], warehouse)
    shutil.copytree(receipt_tree["quality"], quality)
    relocated_path, relocated_descriptor = _freeze_again(
        receipt_tree, tmp_path / "relocated-aux", raw_root=raw,
        warehouse=warehouse, quality=quality)
    assert relocated_descriptor == same
    assert Path(relocated_path).name == Path(same_path).name
    assert not any(str(relocated) in json.dumps(row)
                   for row in relocated_descriptor["objects"])


def test_aux_payloads_and_keys_are_date_exact(receipt_tree):
    descriptor = receipt_tree["aux_descriptor"]
    rows = descriptor["objects"]
    manifest = next(row for row in rows
                    if row["family"] == "manifest_date_projection")
    with (receipt_tree["aux_bundle"] / manifest["local_relpath"]).open(
            newline="") as f:
        manifest_rows = list(csv.DictReader(f))
    assert manifest_rows
    assert {row["date"] for row in manifest_rows} == {DATE}
    assert all(not os.path.isabs(row["file_path"]) for row in manifest_rows)
    assert "date=%s" % DATE in manifest["key"]

    ledger = next(row for row in rows
                  if row["source_kind"] == "corrections_ledger_day")
    ledger_rows = [json.loads(line) for line in (
        receipt_tree["aux_bundle"] / ledger["local_relpath"]
    ).read_text().splitlines()]
    assert ledger_rows
    assert {row["exchange_date"] for row in ledger_rows} == {DATE}
    assert "date=%s" % DATE in ledger["key"]
    assert all(row["expected_version_id"] == "v1"
               and row["storage_mode"] == "EXISTING_EXACT_VERSION"
               and row["local_relpath"] is None
               for row in rows if row["family"] == "catalog_at_cutoff")


@pytest.mark.parametrize("fault", [
    "missing_required", "null_version", "newer_than_cutoff",
    "wrong_key", "path_traversal",
])
def test_catalog_cutoff_bindings_fail_closed(receipt_tree, tmp_path, fault):
    bindings = copy.deepcopy(receipt_tree["catalog_bindings"])
    if fault == "missing_required":
        bindings["objects"].pop()
    elif fault == "null_version":
        bindings["objects"][0]["VersionId"] = "null"
    elif fault == "newer_than_cutoff":
        bindings["objects"][0]["last_modified_utc"] = \
            "2026-07-14T04:00:00Z"
    elif fault == "wrong_key":
        bindings["objects"][0]["key"] = "ec2/warehouse/manifest.csv"
    else:
        bindings["objects"][0]["logical_source_key"] = \
            "warehouse/catalog/../escape"
    with pytest.raises(cr.ReceiptError, match="CATALOG_BINDINGS|UNSAFE_PATH"):
        _freeze_again(
            receipt_tree, tmp_path / ("bad-%s" % fault), bindings=bindings)


def test_pre_resolved_catalog_uses_historical_version_not_latest(receipt_tree,
                                                                 tmp_path):
    _binding, objects = _build(receipt_tree)
    catalog = next(row for row in objects
                   if row["family"] == "catalog_at_cutoff")
    client = FakeExactVersionClient(receipt_tree["payloads"])
    object_id = (BUCKET, catalog["key"])
    client.versions[(BUCKET, catalog["key"], "v2")] = b"new-live-catalog"
    client.version_modified[(BUCKET, catalog["key"], "v2")] = \
        "2026-07-14T03:30:00Z"
    client.latest[object_id] = "v2"
    verified, failures, complete = cr.verify_inventory(
        [catalog], client, str(tmp_path / "historical"))
    assert complete and not failures
    assert verified[0]["VersionId"] == "v1"
    assert ("list_versions", BUCKET, catalog["key"]) in client.ops
    assert ("head", BUCKET, catalog["key"], "v1") in client.ops
    assert ("get_exact", BUCKET, catalog["key"], "v1") in client.ops

    class WrongTimestampClient(FakeExactVersionClient):
        def head(self, bucket, key, version_id=None):
            result = super().head(bucket, key, version_id)
            result["LastModified"] = "2026-07-14T02:31:00Z"
            return result

    bad_client = WrongTimestampClient(receipt_tree["payloads"])
    _verified, failures, complete = cr.verify_inventory(
        [catalog], bad_client, str(tmp_path / "wrong-timestamp"))
    assert not complete
    assert {row["code"] for row in failures} == {"LAST_MODIFIED_MISMATCH"}
    assert not any(op[0] == "get_exact" for op in bad_client.ops)


def test_catalog_binding_must_be_current_at_authenticated_cutoff(receipt_tree,
                                                                 tmp_path):
    _binding, objects = _build(receipt_tree)
    catalog = next(row for row in objects
                   if row["family"] == "catalog_at_cutoff")
    client = FakeExactVersionClient(receipt_tree["payloads"])
    identity = (BUCKET, catalog["key"])
    client.versions[(BUCKET, catalog["key"], "v2")] = b"pre-cutoff-replacement"
    client.version_modified[(BUCKET, catalog["key"], "v2")] = \
        "2026-07-14T02:45:00Z"
    client.latest[identity] = "v2"

    _verified, failures, complete = cr.verify_inventory(
        [catalog], client, str(tmp_path / "stale-cutoff-version"))
    assert not complete
    assert {row["code"] for row in failures} == {"CUTOFF_VERSION_MISMATCH"}
    assert not any(op[0] in {"head", "get_exact"} for op in client.ops)


@pytest.mark.parametrize(("history", "code"), [
    ({"IsTruncated": True, "Versions": [], "DeleteMarkers": []},
     "CUTOFF_HISTORY_INCOMPLETE"),
    ({
        "IsTruncated": False,
        "Versions": [{
            "Key": "__KEY__", "VersionId": "v1",
            "LastModified": "2026-07-14T02:30:00Z",
        }],
        "DeleteMarkers": [{
            "Key": "__KEY__", "VersionId": "delete-v2",
            "LastModified": "2026-07-14T02:45:00Z",
        }],
    }, "CUTOFF_DELETE_MARKER"),
])
def test_catalog_cutoff_history_fail_closed(receipt_tree, tmp_path,
                                            history, code):
    _binding, objects = _build(receipt_tree)
    catalog = next(row for row in objects
                   if row["family"] == "catalog_at_cutoff")

    class HistoryClient(FakeExactVersionClient):
        def list_versions(self, bucket, key):
            self.ops.append(("list_versions", bucket, key))
            payload = copy.deepcopy(history)
            for field in ("Versions", "DeleteMarkers"):
                for row in payload.get(field, []):
                    if row.get("Key") == "__KEY__":
                        row["Key"] = key
            return payload

    client = HistoryClient(receipt_tree["payloads"])
    _verified, failures, complete = cr.verify_inventory(
        [catalog], client, str(tmp_path / code.lower()))
    assert not complete
    assert {row["code"] for row in failures} == {code}


def test_catalog_v2_evidence_is_bidirectional_and_identity_bound(
        receipt_tree, tmp_path):
    base = copy.deepcopy(receipt_tree["catalog_bindings"])

    attacker_bucket = copy.deepcopy(base)
    attacker_bucket["source_release"]["bucket"] = "attacker-controlled-bucket"
    with pytest.raises(cr.ReceiptError, match="trusted bucket"):
        _freeze_again(receipt_tree, tmp_path / "attacker-bucket",
                      bindings=attacker_bucket)

    fake_id = copy.deepcopy(base)
    fake_id["source_release"]["release_id"] = "fake-release"
    with pytest.raises(cr.ReceiptError, match="CATALOG_BINDINGS"):
        _freeze_again(receipt_tree, tmp_path / "fake-release",
                      bindings=fake_id)

    fake_cutoff = copy.deepcopy(base)
    fake_cutoff["cutoff_utc"] = "2099-01-01T00:00:00Z"
    with pytest.raises(cr.ReceiptError, match="CATALOG_BINDINGS"):
        _freeze_again(receipt_tree, tmp_path / "fake-cutoff",
                      bindings=fake_cutoff)

    future_manifest = copy.deepcopy(base)
    manifest = json.loads(Path(
        future_manifest["source_release"]["local_path"]).read_text())
    manifest["generated_at_utc"] = "2099-01-01T00:00:00Z"
    manifest["corrections"]["cutoff_utc"] = manifest["generated_at_utc"]
    payload = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    path = tmp_path / "future-source-MANIFEST.json"
    path.write_bytes(payload)
    future_manifest["source_release"].update({
        "local_path": str(path), "size": len(payload), "sha256": _sha(payload),
    })
    with pytest.raises(cr.ReceiptError, match="predates its generated"):
        _freeze_again(receipt_tree, tmp_path / "future-manifest",
                      bindings=future_manifest)

    omitted_optional = copy.deepcopy(base)
    manifest = json.loads(Path(
        omitted_optional["source_release"]["local_path"]).read_text())
    optional_payload = b"fixture-optional-catalog"
    manifest["objects"].append({
        "key": "catalog/settlements/part-00000.parquet",
        "size": len(optional_payload), "sha256": _sha(optional_payload),
        "version_id": "research-v1",
    })
    manifest["version_binding"]["bindings_sha256"] = cr.canonical_sha256({
        row["key"]: row["version_id"] for row in manifest["objects"]
    })
    manifest["post_upload_verification"]["objects_verified"] = len(
        manifest["objects"])
    payload = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    path = tmp_path / "optional-source-MANIFEST.json"
    path.write_bytes(payload)
    omitted_optional["source_release"].update({
        "local_path": str(path), "size": len(payload), "sha256": _sha(payload),
    })
    with pytest.raises(cr.ReceiptError, match="catalog sets differ"):
        _freeze_again(receipt_tree, tmp_path / "omitted-optional",
                      bindings=omitted_optional)


def test_exact_v2_manifest_is_verified_before_ready(receipt_tree, tmp_path):
    _binding, objects = _build(receipt_tree)
    source = next(row for row in objects
                  if row["family"] == "catalog_cutoff_evidence")
    client = FakeExactVersionClient(
        receipt_tree["payloads"], corrupt_exact_for=source["key"])
    _verified, failures, complete = cr.verify_inventory(
        [source], client, str(tmp_path / "corrupt-source-manifest"))
    assert not complete
    assert {row["code"] for row in failures} == {"SHA256_MISMATCH"}


def test_source_v2_manifest_must_have_one_original_version(receipt_tree,
                                                           tmp_path):
    _binding, objects = _build(receipt_tree)
    source = next(row for row in objects
                  if row["family"] == "catalog_cutoff_evidence")
    client = FakeExactVersionClient(receipt_tree["payloads"])
    client.versions[(BUCKET, source["key"], "v2")] = b"overwritten-manifest"
    client.version_modified[(BUCKET, source["key"], "v2")] = \
        "2026-07-14T04:00:00Z"
    client.latest[(BUCKET, source["key"])] = "v2"
    _verified, failures, complete = cr.verify_inventory(
        [source], client, str(tmp_path / "source-overwrite"))
    assert not complete
    assert {row["code"] for row in failures} == {
        "SOURCE_MANIFEST_NOT_IMMUTABLE"}
    assert not any(op[0] in {"head", "get_exact"} for op in client.ops)


def test_tampered_or_incomplete_aux_set_stops_before_s3(receipt_tree,
                                                        monkeypatch):
    manifest = next(
        row for row in receipt_tree["aux_descriptor"]["objects"]
        if row["family"] == "manifest_date_projection")
    (receipt_tree["aux_bundle"] / manifest["local_relpath"]).write_bytes(
        b"tampered")

    def forbid_s3(_executable):
        pytest.fail("invalid local auxiliary set must stop before S3")

    monkeypatch.setattr(cr, "AwsCliS3Client", forbid_s3)
    output = receipt_tree["warehouse"].parent / "tampered-output"
    rc = cr.main([
        "shadow", "--date", DATE, "--bucket", BUCKET,
        "--prefix", PREFIX,
        "--raw-root", str(receipt_tree["raw_root"]),
        "--warehouse-root", str(receipt_tree["warehouse"]),
        "--quality-dir", str(receipt_tree["quality"]),
        "--aux-bundle", str(receipt_tree["aux_bundle"]),
        "--output-root", str(output),
    ])
    assert rc == 2
    status = json.loads((output / ("date=%s" % DATE)
                         / "SHADOW_STATUS.json").read_text())
    assert status["state"] == "BLOCKED_INTEGRITY"
    assert status["complete"] is False
    assert {row["code"] for row in status["failures"]} == {
        "AUX_SET_INVALID"}


def test_bundle_files_are_read_once_and_retained_for_semantics(
        receipt_tree, monkeypatch):
    calls = []
    real_read = cr._BundleReader.read

    def counted_read(self, rel):
        calls.append(rel)
        return real_read(self, rel)

    monkeypatch.setattr(cr._BundleReader, "read", counted_read)
    _build(receipt_tree)
    declared = [
        row["local_relpath"]
        for row in receipt_tree["aux_descriptor"]["objects"]
        if row["local_relpath"] is not None
    ]
    assert calls.count("AUX_SET.json") == 1
    assert set(calls) == {"AUX_SET.json", *declared}
    assert all(calls.count(rel) == 1 for rel in declared)


def test_bundle_replacement_after_read_fails_closed(receipt_tree, monkeypatch):
    real_read = cr._BundleReader.read
    changed = {"done": False}
    manifest = next(
        row for row in receipt_tree["aux_descriptor"]["objects"]
        if row["family"] == "manifest_date_projection")

    def replace_after_read(self, rel):
        result = real_read(self, rel)
        if rel == manifest["local_relpath"] and not changed["done"]:
            path = receipt_tree["aux_bundle"] / rel
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(path.read_bytes())
            os.replace(replacement, path)
            changed["done"] = True
        return result

    monkeypatch.setattr(cr._BundleReader, "read", replace_after_read)
    with pytest.raises(cr.ReceiptError, match="LOCAL_SOURCE_CHANGED"):
        _build(receipt_tree)


@pytest.mark.parametrize("kind", ["leaf", "intermediate"])
def test_bundle_symlinks_fail_closed(receipt_tree, tmp_path, kind):
    root = tmp_path / "symlink-bundle" / receipt_tree["aux_bundle"].name
    shutil.copytree(receipt_tree["aux_bundle"], root)
    manifest = next(
        row for row in receipt_tree["aux_descriptor"]["objects"]
        if row["family"] == "manifest_date_projection")
    target = root / manifest["local_relpath"]
    if kind == "leaf":
        external = tmp_path / "external-manifest.csv"
        external.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(external)
    else:
        external = tmp_path / "external-manifest-dir"
        shutil.copytree(target.parent, external)
        shutil.rmtree(target.parent)
        target.parent.symlink_to(external, target_is_directory=True)
    with pytest.raises(cr.ReceiptError, match="AUX_SET_INVALID|symlink"):
        cr.load_auxiliary_set(
            str(root), DATE, BUCKET, PREFIX,
            receipt_tree["seal"], _sha(receipt_tree["seal_path"].read_bytes()))


def test_freeze_aux_rejects_corrections_mismatch(receipt_tree, tmp_path):
    ledger = receipt_tree["warehouse"] / "corrections" / "ledger.ndjson"
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    rows[0]["n_rows"] = 2
    ledger.write_text("\n".join(json.dumps(row, sort_keys=True)
                                for row in rows) + "\n")
    with pytest.raises(cr.ReceiptError, match="CORRECTIONS_INVALID"):
        _freeze_again(receipt_tree, tmp_path / "bad-corrections")


@pytest.mark.parametrize("fault", [
    "conflicting_dates", "late_d_plus_1", "different_source",
    "different_observed_at", "invalid_source_rel", "reversed_offsets",
])
def test_corrections_date_and_source_groups_fail_closed(receipt_tree, tmp_path,
                                                        fault):
    late_path = (receipt_tree["warehouse"] / "corrections"
                 / ("date=%s" % DATE) / "late_rows.ndjson")
    ledger_path = (receipt_tree["warehouse"] / "corrections"
                   / "ledger.ndjson")
    late = [json.loads(line) for line in late_path.read_text().splitlines()]
    ledger = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    if fault == "conflicting_dates":
        ledger[0]["exchange_date"] = NEXT_DATE
        ledger[0]["date"] = DATE
    elif fault == "late_d_plus_1":
        late[0]["row"][0] = wc.day_start_us(NEXT_DATE) + 1
    elif fault == "different_source":
        ledger[0]["source_raw_rel"] = "date=%s/l2_23.ndjson.1" % DATE
        ledger[0]["source_file"] = \
            "/fixture/raw/date=%s/l2_23.ndjson.1" % DATE
    elif fault == "different_observed_at":
        ledger[0]["observed_at_utc"] = "2026-07-14T03:00:01Z"
    elif fault == "invalid_source_rel":
        late[0]["source_raw_rel"] = "../escape"
    else:
        late[0]["source_start_offset"] = 101
        late[0]["source_end_offset"] = 100
    late_path.write_text("\n".join(json.dumps(row, sort_keys=True)
                                     for row in late) + "\n")
    ledger_path.write_text("\n".join(json.dumps(row, sort_keys=True)
                                       for row in ledger) + "\n")
    with pytest.raises(cr.ReceiptError, match="CORRECTIONS_INVALID"):
        _freeze_again(receipt_tree, tmp_path / ("bad-%s" % fault))


def test_missing_family_is_declared_and_never_silently_ready(receipt_tree):
    missing = (receipt_tree["warehouse"] / "dim" / "snapshots"
               / ("date=%s" % DATE) / "markets.csv")
    missing.unlink()
    seal_binding, _objects = _build(receipt_tree)
    families = {row["name"]: row for row in seal_binding["families"]}
    assert families["dim_snapshot"]["state"] == "INCOMPLETE"
    assert families["dim_snapshot"]["reason_code"] == "DIM_FILES_MISSING"
    assert cr._family_blockers(seal_binding["families"])


def _mark_capture_receipt_unreadable(tree):
    path = tree["quality"] / ("capture_gap_receipt_%s.json" % DATE)
    payload = json.loads(path.read_text())
    payload["unreadable"] = True
    path.write_text(json.dumps(payload))


def test_freeze_aux_rejects_unreadable_capture_receipt(receipt_tree):
    _mark_capture_receipt_unreadable(receipt_tree)
    with pytest.raises(cr.ReceiptError, match="unreadable"):
        cr.freeze_auxiliary_set(
            DATE, BUCKET, PREFIX, str(receipt_tree["raw_root"]),
            str(receipt_tree["warehouse"]), str(receipt_tree["quality"]),
            str(receipt_tree["warehouse"].parent / "unreadable-aux"),
            receipt_tree["catalog_bindings"])


def test_cli_unreadable_capture_stops_before_any_s3_call(receipt_tree,
                                                         monkeypatch):
    _mark_capture_receipt_unreadable(receipt_tree)
    output = receipt_tree["warehouse"].parent / "unreadable-aux-cli"
    bindings = receipt_tree["warehouse"].parent / "catalog-bindings.json"
    bindings.write_text(json.dumps(receipt_tree["catalog_bindings"]))

    def forbid_s3(_executable):
        pytest.fail("local quality blocker must stop before creating S3 client")

    monkeypatch.setattr(cr, "AwsCliS3Client", forbid_s3)
    rc = cr.main([
        "freeze-aux", "--date", DATE, "--bucket", BUCKET,
        "--prefix", PREFIX,
        "--raw-root", str(receipt_tree["raw_root"]),
        "--warehouse-root", str(receipt_tree["warehouse"]),
        "--quality-dir", str(receipt_tree["quality"]),
        "--catalog-bindings", str(bindings),
        "--aux-root", str(output),
    ])
    assert rc == 2
    assert not list(output.glob("date=*/aux-set=*"))


@pytest.mark.parametrize("field", ["raw_files", "archive_file_stats"])
def test_inventory_rejects_seal_path_traversal(receipt_tree, field):
    seal = copy.deepcopy(receipt_tree["seal"])
    seal[field][0]["file"] = "../outside"
    receipt_tree["seal_path"].write_text(json.dumps(seal))
    with pytest.raises(cr.ReceiptError, match="travers|escape|unsafe|path"):
        _build(receipt_tree)


def test_inventory_rejects_duplicate_canonical_key(receipt_tree):
    seal = copy.deepcopy(receipt_tree["seal"])
    seal["raw_files"].append(copy.deepcopy(seal["raw_files"][0]))
    receipt_tree["seal_path"].write_text(json.dumps(seal))
    with pytest.raises(cr.ReceiptError, match="duplic|collision"):
        _build(receipt_tree)


def test_inventory_rejects_manifest_drift_from_frozen_seal(receipt_tree):
    manifest = receipt_tree["warehouse"] / "manifest.csv"
    text = manifest.read_text()
    manifest.write_text(text.replace(",1,", ",2,", 1))
    with pytest.raises(cr.ReceiptError,
                       match="MANIFEST_DATE_PROJECTION_MISMATCH"):
        _build(receipt_tree)


@pytest.mark.parametrize("bad_rel", [
    "misc/rfq_23.ndjson.2",
    "date=not-a-date/rfq_23.ndjson.2",
    "date=2026-07-13/extra/rfq_23.ndjson.2",
])
def test_inventory_rejects_rfq_outside_real_date_parent(receipt_tree, bad_rel):
    seal = copy.deepcopy(receipt_tree["seal"])
    rfq = next(row for row in seal["raw_files"]
               if "/rfq_23.ndjson.2" in row["file"])
    rfq["file"] = bad_rel
    receipt_tree["seal_path"].write_text(json.dumps(seal))
    with pytest.raises(cr.ReceiptError, match="INVALID_RAW_PATH|INVALID_DATE"):
        _build(receipt_tree)


def test_seal_change_during_authoritative_gate_is_rejected(receipt_tree,
                                                            monkeypatch):
    def mutate_seal(*_args, **_kwargs):
        path = receipt_tree["seal_path"]
        path.write_bytes(path.read_bytes() + b" ")

    monkeypatch.setattr(cr, "_authoritative_seal_gate", mutate_seal)
    with pytest.raises(cr.ReceiptError, match="LOCAL_SOURCE_CHANGED"):
        _build(receipt_tree)


def test_verify_inventory_pins_exact_version_even_if_latest_races(receipt_tree):
    _seal_binding, objects = _build(receipt_tree)
    race_key = next(
        obj["key"] for obj in objects
        if obj["logical_source_key"] == "warehouse/manifest.csv")
    client, verified, failures, complete = _verify_all(
        receipt_tree, objects, race_key=race_key
    )
    assert complete is True
    assert failures == []
    assert len(verified) == len(objects)
    raced = next(obj for obj in verified if obj["key"] == race_key)
    assert raced["VersionId"] == "v1"
    assert raced["durability_verified"] is True
    assert ("get_exact", BUCKET, race_key, "v1") in client.ops


def test_verify_inventory_rejects_null_version_id(receipt_tree):
    _seal_binding, objects = _build(receipt_tree)
    bad_key = objects[0]["key"]
    _client, verified, failures, complete = _verify_all(
        receipt_tree, objects, missing_version_for=bad_key
    )
    assert complete is False
    assert failures
    assert all(obj["key"] != bad_key for obj in verified)
    assert "version" in json.dumps(failures).lower()


def test_verify_inventory_rejects_exact_version_sha_mismatch(receipt_tree):
    _seal_binding, objects = _build(receipt_tree)
    bad_key = objects[0]["key"]
    _client, verified, failures, complete = _verify_all(
        receipt_tree, objects, corrupt_exact_for=bad_key
    )
    assert complete is False
    assert failures
    assert all(obj["key"] != bad_key for obj in verified)
    assert "sha" in json.dumps(failures).lower()


def test_verify_inventory_rejects_size_mismatch_before_download(receipt_tree):
    _seal_binding, objects = _build(receipt_tree)
    bad_key = objects[0]["key"]
    payloads = dict(receipt_tree["payloads"])
    payloads[bad_key] += b"extra"
    client = FakeExactVersionClient(payloads)
    verified, failures, complete = cr.verify_inventory(
        objects, client, str(receipt_tree["warehouse"].parent / "verify_tmp")
    )
    assert complete is False and failures
    assert all(obj["key"] != bad_key for obj in verified)
    assert not any(op[0] == "get_exact" and op[2] == bad_key for op in client.ops)
    assert "size" in json.dumps(failures).lower()


def test_metadata_only_and_probe_limit_are_explicitly_non_authoritative(receipt_tree):
    seal_binding, objects = _build(receipt_tree)
    client = FakeExactVersionClient(receipt_tree["payloads"])
    metadata, failures, complete = cr.verify_inventory(
        objects, client, str(receipt_tree["warehouse"].parent / "meta_tmp"),
        metadata_only=True,
    )
    assert failures == []
    assert complete is False
    assert not any(op[0] == "get_exact" for op in client.ops)
    with pytest.raises(cr.ReceiptError, match="partial|complete|authoritative|verified"):
        cr.write_shadow_receipt(
            str(receipt_tree["warehouse"].parent / "receipts"), DATE,
            seal_binding, metadata, "2026-07-14T03:00:00Z", "b" * 40,
        )

    client = FakeExactVersionClient(receipt_tree["payloads"])
    partial, failures, complete = cr.verify_inventory(
        objects, client, str(receipt_tree["warehouse"].parent / "probe_tmp"),
        probe_limit=1,
    )
    assert failures == []
    assert complete is False
    assert len(partial) == 1
    with pytest.raises(cr.ReceiptError,
                       match="partial|complete|authoritative|verified"):
        cr.write_shadow_receipt(
            str(receipt_tree["warehouse"].parent / "receipts"), DATE,
            seal_binding, partial, "2026-07-14T03:00:00Z", "b" * 40,
        )
    assert not list((receipt_tree["warehouse"].parent / "receipts").glob(
        "date=*/receipt-*.json"
    ))

    # A caller cannot turn a successful full verification into a valid
    # receipt merely by slicing objects out of the returned list.
    _client, full, failures, complete = _verify_all(receipt_tree, objects)
    assert complete and not failures
    with pytest.raises(cr.ReceiptError,
                       match="partial|complete|authoritative|verified"):
        cr.write_shadow_receipt(
            str(receipt_tree["warehouse"].parent / "receipts"), DATE,
            seal_binding, full[:-1], "2026-07-14T03:00:00Z", "b" * 40,
        )


def test_stable_digest_ignores_time_and_order_but_binds_semantics(receipt_tree):
    seal_binding, objects = _build(receipt_tree)
    _client, verified, failures, complete = _verify_all(receipt_tree, objects)
    assert complete and not failures

    a = copy.deepcopy(verified)
    b = list(reversed(copy.deepcopy(verified)))
    for obj in a:
        obj["verified_at_utc"] = "2026-07-14T03:00:00Z"
        obj["publisher_commit"] = "a" * 40
    for obj in b:
        obj["verified_at_utc"] = "2030-01-01T00:00:00Z"
        obj["publisher_commit"] = "f" * 40

    pa = cr.stable_receipt_projection(DATE, seal_binding, a)
    pb = cr.stable_receipt_projection(DATE, seal_binding, b)
    assert pa == pb
    assert cr.receipt_set_sha256(DATE, seal_binding, a) == \
        cr.receipt_set_sha256(DATE, seal_binding, b)

    changed = copy.deepcopy(a)
    changed[0]["channel"] = "different-channel"
    assert cr.receipt_set_sha256(DATE, seal_binding, changed) != \
        cr.receipt_set_sha256(DATE, seal_binding, a)
    changed = copy.deepcopy(a)
    changed[0]["eligibility_tag_state"] = "TAGGED_VERIFIED"
    assert cr.receipt_set_sha256(DATE, seal_binding, changed) != \
        cr.receipt_set_sha256(DATE, seal_binding, a)


def test_shadow_receipt_is_write_once_idempotent_and_time_independent(receipt_tree):
    seal_binding, objects = _build(receipt_tree)
    _client, verified, failures, complete = _verify_all(receipt_tree, objects)
    assert complete and not failures
    out = receipt_tree["warehouse"].parent / "receipts"

    first = Path(cr.write_shadow_receipt(
        str(out), DATE, seal_binding, verified,
        "2026-07-14T03:00:00Z", "b" * 40,
    ))
    before = first.read_bytes()
    second = Path(cr.write_shadow_receipt(
        str(out), DATE, seal_binding, list(reversed(verified)),
        "2030-01-01T00:00:00Z", "c" * 40,
    ))
    assert first == second
    assert first.read_bytes() == before
    assert first.parent.name == "date=%s" % DATE
    assert first.name == "receipt-%s.json" % cr.receipt_set_sha256(
        DATE, seal_binding, verified
    )

    receipt = json.loads(before)
    assert receipt["date"] == DATE
    assert receipt["verified_at_utc"] == "2026-07-14T03:00:00Z"
    assert receipt["publisher_code_commit"] == "b" * 40
    assert receipt["receipt_set_sha256"] == \
        cr.receipt_set_sha256(DATE, seal_binding, verified)
    assert len(receipt["objects"]) == len(verified)
    assert all(obj["VersionId"] == "v1" for obj in receipt["objects"])

    # Same digest key with altered bytes is not repaired or overwritten.
    first.write_text("{}\n")
    with pytest.raises(cr.ReceiptError, match="(?i)conflict|exist|immutable|mismatch"):
        cr.write_shadow_receipt(
            str(out), DATE, seal_binding, verified,
            "2026-07-14T03:00:00Z", "b" * 40,
        )
    assert first.read_text() == "{}\n"


@pytest.mark.parametrize(("field", "unsafe"), [
    ("authoritative", True),
    ("s3_published", True),
    ("prune_eligible", True),
    ("state", "AUTHORITATIVE"),
])
def test_shadow_receipt_refuses_reuse_with_unsafe_flags(receipt_tree,
                                                        field, unsafe):
    seal_binding, objects = _build(receipt_tree)
    _client, verified, failures, complete = _verify_all(receipt_tree, objects)
    assert complete and not failures
    out = receipt_tree["warehouse"].parent / "receipts"
    path = Path(cr.write_shadow_receipt(
        str(out), DATE, seal_binding, verified,
        "2026-07-14T03:00:00Z", "b" * 40,
    ))
    payload = json.loads(path.read_text())
    payload[field] = unsafe
    path.write_text(json.dumps(payload))
    with pytest.raises(cr.ReceiptError, match="RECEIPT_CONFLICT"):
        cr.write_shadow_receipt(
            str(out), DATE, seal_binding, verified,
            "2030-01-01T00:00:00Z", "c" * 40,
        )


def test_cli_receipt_conflict_overwrites_ready_status_with_blocked(
        receipt_tree, monkeypatch):
    output = receipt_tree["warehouse"].parent / "cli-shadow"
    monkeypatch.setattr(cr, "_code_commit", lambda: "c" * 40)

    def client_factory(_executable):
        return FakeExactVersionClient(receipt_tree["payloads"])

    monkeypatch.setattr(cr, "AwsCliS3Client", client_factory)
    argv = [
        "shadow", "--date", DATE, "--bucket", BUCKET,
        "--prefix", PREFIX,
        "--raw-root", str(receipt_tree["raw_root"]),
        "--warehouse-root", str(receipt_tree["warehouse"]),
        "--quality-dir", str(receipt_tree["quality"]),
        "--aux-bundle", str(receipt_tree["aux_bundle"]),
        "--output-root", str(output),
    ]
    assert cr.main(argv) == 0
    receipt = next(output.glob("date=*/receipt-*.json"))
    payload = json.loads(receipt.read_text())
    payload["authoritative"] = True
    receipt.write_text(json.dumps(payload))

    assert cr.main(argv) == 2
    status = json.loads((output / ("date=%s" % DATE)
                         / "SHADOW_STATUS.json").read_text())
    assert status["state"] == "BLOCKED_INTEGRITY"
    assert status["complete"] is False
    assert any(row["code"] == "RECEIPT_CONFLICT"
               for row in status["failures"])


def test_cli_early_precheck_failure_overwrites_previous_ready(
        receipt_tree, monkeypatch):
    output = receipt_tree["warehouse"].parent / "stale-ready-shadow"
    monkeypatch.setattr(cr, "_code_commit", lambda: "c" * 40)
    monkeypatch.setattr(
        cr, "AwsCliS3Client",
        lambda _executable: FakeExactVersionClient(receipt_tree["payloads"]))
    argv = [
        "shadow", "--date", DATE, "--bucket", BUCKET,
        "--prefix", PREFIX,
        "--raw-root", str(receipt_tree["raw_root"]),
        "--warehouse-root", str(receipt_tree["warehouse"]),
        "--quality-dir", str(receipt_tree["quality"]),
        "--aux-bundle", str(receipt_tree["aux_bundle"]),
        "--output-root", str(output),
    ]
    assert cr.main(argv) == 0
    status_path = output / ("date=%s" % DATE) / "SHADOW_STATUS.json"
    assert json.loads(status_path.read_text())["complete"] is True

    manifest = next(
        row for row in receipt_tree["aux_descriptor"]["objects"]
        if row["family"] == "manifest_date_projection")
    (receipt_tree["aux_bundle"] / manifest["local_relpath"]).write_bytes(
        b"tampered-after-ready")
    assert cr.main(argv) == 2
    status = json.loads(status_path.read_text())
    assert status["state"] == "BLOCKED_INTEGRITY"
    assert status["complete"] is False
    assert status["content_verification_complete"] is False
    assert {row["code"] for row in status["failures"]} == {
        "AUX_SET_INVALID"}


def test_cli_invalidates_ready_before_precheck_can_interrupt(
        receipt_tree, monkeypatch):
    output = receipt_tree["warehouse"].parent / "interrupted-shadow"
    status_path = output / ("date=%s" % DATE) / "SHADOW_STATUS.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(json.dumps({
        "schema_version": cr.SHADOW_STATUS_SCHEMA,
        "state": "RECEIPT_VERIFIED_SHADOW", "complete": True,
    }))

    def interrupt(*_args, **_kwargs):
        running = json.loads(status_path.read_text())
        assert running["state"] == "SHADOW_RUNNING"
        assert running["complete"] is False
        raise KeyboardInterrupt()

    monkeypatch.setattr(cr, "build_desired_inventory", interrupt)
    with pytest.raises(KeyboardInterrupt):
        cr.main([
            "shadow", "--date", DATE, "--bucket", BUCKET,
            "--prefix", PREFIX,
            "--raw-root", str(receipt_tree["raw_root"]),
            "--warehouse-root", str(receipt_tree["warehouse"]),
            "--quality-dir", str(receipt_tree["quality"]),
            "--aux-bundle", str(receipt_tree["aux_bundle"]),
            "--output-root", str(output),
        ])
    status = json.loads(status_path.read_text())
    assert status["state"] == "SHADOW_RUNNING"
    assert status["complete"] is False


def test_cli_missing_planned_quality_is_explicitly_pending(receipt_tree,
                                                           monkeypatch):
    output = receipt_tree["warehouse"].parent / "quality-pending"
    missing = "%s/control/quality/v1/date=%s/l2_gaps.json" % (PREFIX, DATE)
    client = FakeExactVersionClient(
        receipt_tree["payloads"], missing_key=missing)
    monkeypatch.setattr(cr, "AwsCliS3Client", lambda _executable: client)
    rc = cr.main([
        "shadow", "--date", DATE, "--bucket", BUCKET,
        "--prefix", PREFIX,
        "--raw-root", str(receipt_tree["raw_root"]),
        "--warehouse-root", str(receipt_tree["warehouse"]),
        "--quality-dir", str(receipt_tree["quality"]),
        "--aux-bundle", str(receipt_tree["aux_bundle"]),
        "--output-root", str(output),
    ])
    assert rc == 3
    status = json.loads((output / ("date=%s" % DATE)
                         / "SHADOW_STATUS.json").read_text())
    assert status["state"] == "PENDING_QUALITY_CANONICAL"
    assert status["complete"] is False
    assert not list(output.glob("date=*/receipt-*.json"))
    assert {op[0] for op in client.ops} <= {
        "list_versions", "head", "get_exact"}


def test_write_atomic_json_preserves_existing_file_if_replace_fails(tmp_path,
                                                                    monkeypatch):
    target = tmp_path / "index.json"
    target.write_text('{"old":true}\n')
    real_replace = cr.os.replace

    def fail_replace(_src, _dst):
        raise OSError("fixture replace failure")

    monkeypatch.setattr(cr.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failure"):
        cr.write_atomic_json(str(target), {"new": True})
    assert target.read_text() == '{"old":true}\n'

    monkeypatch.setattr(cr.os, "replace", real_replace)
    cr.write_atomic_json(str(target), {"new": True})
    assert json.loads(target.read_text()) == {"new": True}


def test_aws_cli_adapter_argv_is_read_only_and_exact_version(monkeypatch,
                                                             tmp_path):
    calls = []

    class Result:
        returncode = 0
        stderr = ""
        stdout = '{"VersionId":"v1","ContentLength":3}'

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return Result()

    monkeypatch.setattr(cr.subprocess, "run", fake_run)
    client = cr.AwsCliS3Client("aws-fixture")
    client.head(BUCKET, "ec2/example")
    client.head(BUCKET, "ec2/example", "v-historical")
    client.get_exact(BUCKET, "ec2/example", "v1", str(tmp_path / "object"))
    client.list_versions(BUCKET, "ec2/example")

    assert calls[0][0:3] == ["aws-fixture", "s3api", "head-object"]
    assert calls[1][0:3] == ["aws-fixture", "s3api", "head-object"]
    assert calls[1][calls[1].index("--version-id") + 1] == "v-historical"
    assert calls[2][0:3] == ["aws-fixture", "s3api", "get-object"]
    assert "--version-id" in calls[2]
    assert calls[2][calls[2].index("--version-id") + 1] == "v1"
    assert calls[3][0:3] == ["aws-fixture", "s3api", "list-object-versions"]
    assert calls[3][calls[3].index("--prefix") + 1] == "ec2/example"
    assert {call[2] for call in calls} == {
        "head-object", "get-object", "list-object-versions"}


def test_shadow_verifier_has_no_s3_mutation_surface(receipt_tree):
    _seal_binding, objects = _build(receipt_tree)
    client, _verified, failures, complete = _verify_all(receipt_tree, objects)
    assert complete and not failures
    assert {op[0] for op in client.ops} <= {
        "list_versions", "head", "get_exact"}

    source = inspect.getsource(cr).lower().replace("_", "-")
    for forbidden in (
        "put-object", "copy-object", "create-multipart-upload",
        "put-object-tagging", "put-object-version-tagging", "delete-object",
        "delete-objects", "put-bucket-lifecycle", "put-object-legal-hold",
    ):
        assert forbidden not in source


def _forward_bundle(tree, root):
    path, descriptor = fcr.freeze_forward_auxiliary_set(
        DATE, BUCKET, PREFIX, str(tree["raw_root"]),
        str(tree["warehouse"]), str(tree["quality"]), str(root))
    payloads = dict(tree["payloads"])
    for row in descriptor["objects"]:
        if row["local_relpath"] is not None:
            payloads[row["key"]] = (
                Path(path) / row["local_relpath"]
            ).read_bytes()
    return Path(path), descriptor, payloads


def test_forward_aux_is_deterministic_and_copies_zero_catalog_bytes(
        receipt_tree, tmp_path):
    first, descriptor, _payloads = _forward_bundle(
        receipt_tree, tmp_path / "forward-a")
    second, descriptor2, _payloads2 = _forward_bundle(
        receipt_tree, tmp_path / "forward-b")
    assert descriptor["mode"] == "FORWARD_CANONICAL_CAPTURE"
    assert descriptor["aux_set_sha256"] == descriptor2["aux_set_sha256"]
    assert first.name == second.name
    catalog = [row for row in descriptor["objects"]
               if row["family"] == "catalog_at_publication"]
    assert catalog
    assert all(row["storage_mode"] == "LOCAL_HASH_ATTESTATION"
               and row["local_relpath"] is None
               and row["expected_version_id"] is None
               for row in catalog)
    local_files = {path.relative_to(first).as_posix()
                   for path in first.rglob("*") if path.is_file()}
    assert not any(path.startswith("catalog/") for path in local_files)
    assert not any(row["family"] == "catalog_cutoff_evidence"
                   for row in descriptor["objects"])


def test_forward_inventory_defers_rfq_by_default_but_preserves_capability(
        receipt_tree, tmp_path):
    bundle, _descriptor, _payloads = _forward_bundle(
        receipt_tree, tmp_path / "forward")
    seal_binding, objects = fcr.build_forward_inventory(
        DATE, BUCKET, PREFIX, str(receipt_tree["raw_root"]),
        str(receipt_tree["warehouse"]), str(receipt_tree["quality"]),
        str(bundle))
    families = {row["name"]: row for row in seal_binding["families"]}
    assert families["catalog_at_publication"]["state"] == "PRESENT_VERIFIED"
    assert "catalog_cutoff_evidence" not in families
    assert not cr._family_blockers(seal_binding["families"])
    rfq = [row for row in objects if row.get("channel") == "rfq"]
    assert rfq == []
    assert families["raw_rfq_deferred"]["state"] == "NOT_APPLICABLE"
    assert families["raw_rfq_deferred"]["reason_code"] == \
        "RFQ_BRANCH_CLOSED_NO_REPAIR"

    _future_binding, future_objects = fcr.build_forward_inventory(
        DATE, BUCKET, PREFIX, str(receipt_tree["raw_root"]),
        str(receipt_tree["warehouse"]), str(receipt_tree["quality"]),
        str(bundle), include_rfq_durability=True)
    rfq = [row for row in future_objects if row.get("channel") == "rfq"]
    assert rfq
    assert all(row["research_candidate"] is False
               and row["research_eligible"] is False
               and row["required"] is False
               and row["exposure_policy"] == "FORBIDDEN_RFQ_DEFAULT"
               for row in rfq)
    other_raw = [row for row in objects
                 if row["family"] == "raw_durability"
                 and row.get("channel") != "rfq"]
    assert other_raw and all(row["required"] is True for row in other_raw)
    catalog = [row for row in objects
               if row["family"] == "catalog_at_publication"]
    assert catalog and all("_expected_version_id" not in row for row in catalog)


def test_forward_full_verification_captures_version_and_last_modified(
        receipt_tree, tmp_path):
    bundle, _descriptor, payloads = _forward_bundle(
        receipt_tree, tmp_path / "forward")
    seal_binding, objects = fcr.build_forward_inventory(
        DATE, BUCKET, PREFIX, str(receipt_tree["raw_root"]),
        str(receipt_tree["warehouse"]), str(receipt_tree["quality"]),
        str(bundle))
    client = FakeExactVersionClient(payloads)
    verified, failures, complete = cr.verify_inventory(
        objects, client, str(tmp_path / "exact"))
    assert complete and failures == []
    assert all(row["VersionId"] == "v1" for row in verified)
    assert all(row["last_modified_utc"].endswith("Z") for row in verified)
    receipt = Path(cr.write_shadow_receipt(
        str(tmp_path / "receipts"), DATE, seal_binding, verified,
        "2026-07-14T04:00:00Z", "b" * 40))
    payload = json.loads(receipt.read_text())
    assert payload["state"] == "RECEIPT_VERIFIED_SHADOW"
    assert all(row.get("last_modified_utc") for row in payload["objects"])


def test_forward_verification_requires_last_modified(receipt_tree, tmp_path):
    bundle, _descriptor, payloads = _forward_bundle(
        receipt_tree, tmp_path / "forward")
    _seal_binding, objects = fcr.build_forward_inventory(
        DATE, BUCKET, PREFIX, str(receipt_tree["raw_root"]),
        str(receipt_tree["warehouse"]), str(receipt_tree["quality"]),
        str(bundle))
    bad_key = objects[0]["key"]
    client = FakeExactVersionClient(
        payloads, missing_last_modified_for=bad_key)
    verified, failures, complete = cr.verify_inventory(
        objects, client, str(tmp_path / "exact"))
    assert not complete and failures
    assert all(row["key"] != bad_key for row in verified)
    assert any(row["code"] == "LAST_MODIFIED_REQUIRED" for row in failures)


def test_forward_freeze_rejects_missing_l2_quality(receipt_tree, tmp_path):
    (receipt_tree["quality"] / ("l2_gaps_%s.json" % DATE)).unlink()
    with pytest.raises(cr.ReceiptError, match="L2 quality"):
        fcr.freeze_forward_auxiliary_set(
            DATE, BUCKET, PREFIX, str(receipt_tree["raw_root"]),
            str(receipt_tree["warehouse"]), str(receipt_tree["quality"]),
            str(tmp_path / "forward"))


def test_forward_bundle_rejects_catalog_digest_tamper(receipt_tree, tmp_path):
    bundle, descriptor, _payloads = _forward_bundle(
        receipt_tree, tmp_path / "forward")
    path = bundle / "AUX_SET.json"
    tampered = copy.deepcopy(descriptor)
    row = next(item for item in tampered["objects"]
               if item["family"] == "catalog_at_publication")
    row["sha256"] = "0" * 64
    projection = {key: value for key, value in tampered.items()
                  if key != "aux_set_sha256"}
    tampered["aux_set_sha256"] = cr.canonical_sha256(projection)
    path.write_text(json.dumps(tampered, sort_keys=True, indent=2) + "\n")
    renamed = bundle.with_name("aux-set=%s" % tampered["aux_set_sha256"])
    bundle.rename(renamed)
    with pytest.raises(cr.ReceiptError, match="catalog|desired set|digest"):
        fcr.load_forward_auxiliary_set(
            str(renamed), DATE, BUCKET, PREFIX, receipt_tree["seal"],
            cr.sha256_file(str(receipt_tree["seal_path"])))


def test_forward_bundle_rejects_catalog_table_semantic_tamper(
        receipt_tree, tmp_path):
    bundle, descriptor, _payloads = _forward_bundle(
        receipt_tree, tmp_path / "forward")
    tampered = copy.deepcopy(descriptor)
    row = next(item for item in tampered["objects"]
               if item["family"] == "catalog_at_publication")
    row["table"] = "forged_table"
    projection = {key: value for key, value in tampered.items()
                  if key != "aux_set_sha256"}
    tampered["aux_set_sha256"] = cr.canonical_sha256(projection)
    (bundle / "AUX_SET.json").write_text(
        json.dumps(tampered, sort_keys=True, indent=2) + "\n")
    renamed = bundle.with_name("aux-set=%s" % tampered["aux_set_sha256"])
    bundle.rename(renamed)
    with pytest.raises(cr.ReceiptError, match="catalog semantics"):
        fcr.load_forward_auxiliary_set(
            str(renamed), DATE, BUCKET, PREFIX, receipt_tree["seal"],
            cr.sha256_file(str(receipt_tree["seal_path"])))


def test_forward_bundle_rejects_control_logical_path_forgery(
        receipt_tree, tmp_path):
    bundle, descriptor, _payloads = _forward_bundle(
        receipt_tree, tmp_path / "forward")
    tampered = copy.deepcopy(descriptor)
    row = next(item for item in tampered["objects"]
               if item["family"] == "capture_gap_receipt")
    row["logical_source_key"] = "warehouse/facts/injected.parquet"
    desired = [{
        key: item[key] for key in (
            "bucket", "key", "logical_source_key", "source_kind", "family",
            "date", "size", "sha256", "seal_binding", "evidence_binding",
            "required", "durability_scope", "research_candidate",
            "exposure_policy", "version_resolution", "canonical_source")
    } for item in tampered["objects"]]
    desired.sort(key=lambda item: (
        item["logical_source_key"], item["bucket"], item["key"]))
    tampered["desired_set_sha256"] = cr.canonical_sha256(desired)
    projection = {key: value for key, value in tampered.items()
                  if key != "aux_set_sha256"}
    tampered["aux_set_sha256"] = cr.canonical_sha256(projection)
    (bundle / "AUX_SET.json").write_text(
        json.dumps(tampered, sort_keys=True, indent=2) + "\n")
    renamed = bundle.with_name("aux-set=%s" % tampered["aux_set_sha256"])
    bundle.rename(renamed)
    with pytest.raises(cr.ReceiptError, match="contract mismatch"):
        fcr.load_forward_auxiliary_set(
            str(renamed), DATE, BUCKET, PREFIX, receipt_tree["seal"],
            cr.sha256_file(str(receipt_tree["seal_path"])))


def test_forward_freeze_rejects_gap_outside_date(receipt_tree, tmp_path):
    path = receipt_tree["quality"] / ("capture_gap_receipt_%s.json" % DATE)
    receipt = json.loads(path.read_text())
    receipt["gaps"] = [{
        "start_us": wc.day_start_us(DATE) - 1,
        "end_us": wc.day_start_us(DATE) + 10,
    }]
    path.write_text(json.dumps(receipt))
    with pytest.raises(cr.ReceiptError, match="outside the sealed date"):
        fcr.freeze_forward_auxiliary_set(
            DATE, BUCKET, PREFIX, str(receipt_tree["raw_root"]),
            str(receipt_tree["warehouse"]), str(receipt_tree["quality"]),
            str(tmp_path / "forward"))


def test_forward_freeze_rejects_malformed_gap_row_instead_of_skipping(
        receipt_tree, tmp_path):
    start = wc.day_start_us(DATE)
    _write(
        receipt_tree["quality"] / "capture_gaps.csv",
        "start_us,end_us\n%d,%d\nnot-an-int,%d\n" %
        (start + 100, start + 200, start + 300))
    with pytest.raises(cr.ReceiptError, match="row 3 is malformed"):
        fcr.freeze_forward_auxiliary_set(
            DATE, BUCKET, PREFIX, str(receipt_tree["raw_root"]),
            str(receipt_tree["warehouse"]), str(receipt_tree["quality"]),
            str(tmp_path / "forward"))


class FakeConditionalWriter:
    def __init__(self):
        self.calls = []
        self.puts = 0
        self.reused = 0

    def ensure_bytes(self, bucket, key, payload):
        self.calls.append((bucket, key, payload))
        self.puts += 1
        return {
            "bucket": bucket,
            "key": key,
            "VersionId": "control-v1",
            "size": len(payload),
            "sha256": _sha(payload),
            "last_modified_utc": "2026-07-14T04:00:00Z",
            "verification_state": "EXACT_VERSION_FULL_SHA256",
        }

    def ensure_equivalent_bytes(self, bucket, key, payload, validator):
        binding = self.ensure_bytes(bucket, key, payload)
        assert validator(payload)
        return binding, payload


class ReusingConditionalWriter(FakeConditionalWriter):
    def __init__(self):
        super().__init__()
        self.stored = None
        self.reused = 0

    def ensure_equivalent_bytes(self, bucket, key, payload, validator):
        if self.stored is None:
            self.stored = payload
            return self.ensure_bytes(bucket, key, payload), payload
        assert validator(self.stored)
        self.reused += 1
        self.calls.append((bucket, key, payload))
        return {
            "bucket": bucket,
            "key": key,
            "VersionId": "control-v1",
            "size": len(self.stored),
            "sha256": _sha(self.stored),
            "last_modified_utc": "2026-07-14T04:00:00Z",
            "verification_state": "EXACT_VERSION_FULL_SHA256",
        }, self.stored


def _forward_shadow(tree, tmp_path):
    bundle, _descriptor, payloads = _forward_bundle(
        tree, tmp_path / "forward")
    seal_binding, objects = fcr.build_forward_inventory(
        DATE, BUCKET, PREFIX, str(tree["raw_root"]),
        str(tree["warehouse"]), str(tree["quality"]), str(bundle))
    client = FakeExactVersionClient(payloads)
    verified, failures, complete = cr.verify_inventory(
        objects, client, str(tmp_path / "exact"))
    assert complete and failures == []
    path = cr.write_shadow_receipt(
        str(tmp_path / "shadow"), DATE, seal_binding, verified,
        "2026-07-14T04:00:00Z", "b" * 40)
    return bundle, Path(path), seal_binding, verified


def test_small_control_sync_uploads_only_allowlisted_retained_bytes(
        receipt_tree, tmp_path):
    bundle, _descriptor, _payloads = _forward_bundle(
        receipt_tree, tmp_path / "forward")
    writer = FakeConditionalWriter()
    path, result = crc.sync_small_controls(
        DATE, BUCKET, PREFIX, str(receipt_tree["raw_root"]),
        str(receipt_tree["warehouse"]), str(receipt_tree["quality"]),
        str(bundle), writer, str(tmp_path / "control-index"))
    assert path.is_file()
    assert result["state"] == "CANONICAL_CONTROLS_VERIFIED"
    assert result["large_data_upload_bytes"] == 0
    assert writer.calls
    keys = [key for _bucket, key, _payload in writer.calls]
    assert all("/publication-snapshots/" in key
               or "/control/quality/" in key for key in keys)
    assert not any("/facts/" in key or "/raw/" in key
                   or "/catalog/" in key or "/dim/" in key for key in keys)
    assert all("/sha256=" in key for key in keys)


def test_small_control_sync_stops_before_put_when_total_exceeds_limit(
        receipt_tree, tmp_path, monkeypatch):
    bundle, _descriptor, _payloads = _forward_bundle(
        receipt_tree, tmp_path / "forward")
    writer = FakeConditionalWriter()
    monkeypatch.setattr(crc, "MAX_CONTROL_TOTAL_BYTES", 1)
    with pytest.raises(cr.ReceiptError, match="CONTROL_TOO_LARGE"):
        crc.sync_small_controls(
            DATE, BUCKET, PREFIX, str(receipt_tree["raw_root"]),
            str(receipt_tree["warehouse"]), str(receipt_tree["quality"]),
            str(bundle), writer, str(tmp_path / "control-index"))
    assert writer.calls == []


def test_durable_receipt_is_new_payload_and_conditional_writer_target(
        receipt_tree, tmp_path):
    bundle, shadow_path, seal_binding, verified = _forward_shadow(
        receipt_tree, tmp_path)
    shadow_before = shadow_path.read_bytes()
    writer = FakeConditionalWriter()
    index_path, index = crc.publish_durable_receipt(
        DATE, BUCKET, PREFIX, writer, str(tmp_path / "durable"),
        seal_binding=seal_binding, verified_objects=verified,
        verified_at="2026-07-14T04:00:00Z",
        publisher_code_commit="b" * 40,
        raw_root=str(receipt_tree["raw_root"]),
        warehouse_root=str(receipt_tree["warehouse"]),
        quality_dir=str(receipt_tree["quality"]), aux_bundle=str(bundle))
    assert index_path.is_file()
    assert shadow_path.read_bytes() == shadow_before
    assert len(writer.calls) == 1
    bucket, key, body = writer.calls[0]
    assert bucket == BUCKET
    assert key == "%s/control/canonical-receipts/v1/date=%s/receipt-%s.json" % (
        PREFIX, DATE, index["receipt_set_sha256"])
    durable = json.loads(body)
    assert durable["state"] == "DURABLE_RECEIPT_VERIFIED"
    assert durable["authority"] == "CANONICAL_CONTROL_PLANE"
    assert durable["authoritative"] is True
    assert durable["s3_published"] is True
    assert durable["prune_eligible"] is False
    assert index["receipt_object"]["VersionId"] == "control-v1"


def test_durable_writer_rejects_in_process_object_scope_spoof(
        receipt_tree, tmp_path):
    bundle, _shadow_path, seal_binding, verified = _forward_shadow(
        receipt_tree, tmp_path)
    target = next(row for row in verified if row["source_kind"] != "seal")
    target["key"] = "%s/control/quality/v1/injected.bin" % PREFIX
    writer = FakeConditionalWriter()
    with pytest.raises(cr.ReceiptError, match="forward authority"):
        crc.publish_durable_receipt(
            DATE, BUCKET, PREFIX, writer, str(tmp_path / "durable"),
            seal_binding=seal_binding, verified_objects=verified,
            verified_at="2026-07-14T04:00:00Z",
            publisher_code_commit="b" * 40,
            raw_root=str(receipt_tree["raw_root"]),
            warehouse_root=str(receipt_tree["warehouse"]),
            quality_dir=str(receipt_tree["quality"]),
            aux_bundle=str(bundle))
    assert writer.calls == []


def test_authoritative_cli_does_not_promote_editable_shadow_json():
    source = inspect.getsource(crc.main)
    assert "--shadow-receipt" not in source
    assert "verify_inventory" in source


def test_durable_writer_rejects_partial_or_metadata_object(
        receipt_tree, tmp_path):
    bundle, _shadow_path, seal_binding, verified = _forward_shadow(
        receipt_tree, tmp_path)
    verified[0]["verification_state"] = "METADATA_ONLY"
    writer = FakeConditionalWriter()
    with pytest.raises(cr.ReceiptError, match="exact-version byte verified"):
        crc.publish_durable_receipt(
            DATE, BUCKET, PREFIX, writer, str(tmp_path / "durable"),
            seal_binding=seal_binding, verified_objects=verified,
            verified_at="2026-07-14T04:00:00Z",
            publisher_code_commit="b" * 40,
            raw_root=str(receipt_tree["raw_root"]),
            warehouse_root=str(receipt_tree["warehouse"]),
            quality_dir=str(receipt_tree["quality"]),
            aux_bundle=str(bundle))
    assert writer.calls == []


def test_durable_receipt_reuses_first_writer_metadata_across_output_roots(
        receipt_tree, tmp_path):
    bundle, _shadow_path, seal_binding, verified = _forward_shadow(
        receipt_tree, tmp_path)
    writer = ReusingConditionalWriter()
    common = dict(
        date=DATE, bucket=BUCKET, prefix=PREFIX, writer=writer,
        seal_binding=seal_binding, verified_objects=verified,
        publisher_code_commit="b" * 40,
        raw_root=str(receipt_tree["raw_root"]),
        warehouse_root=str(receipt_tree["warehouse"]),
        quality_dir=str(receipt_tree["quality"]), aux_bundle=str(bundle))
    _path1, first = crc.publish_durable_receipt(
        output_root=str(tmp_path / "durable-1"),
        verified_at="2026-07-14T04:00:00Z", **common)
    path2, second = crc.publish_durable_receipt(
        output_root=str(tmp_path / "durable-2"),
        verified_at="2026-07-15T05:00:00Z", **common)
    stored = json.loads(
        path2.with_name("receipt-%s.json" %
                        second["receipt_set_sha256"]).read_text())
    assert first["receipt_set_sha256"] == second["receipt_set_sha256"]
    assert stored["verified_at_utc"] == "2026-07-14T04:00:00Z"
    assert writer.puts == 1
    assert writer.reused == 1


def test_conditional_create_success_rejects_hidden_prior_version_or_delete(
        monkeypatch):
    writer = crc.AwsCliConditionalWriter("aws")

    class Result:
        returncode = 0
        stdout = '{"VersionId":"new-v1"}'
        stderr = ""

    class Reader:
        @staticmethod
        def list_versions(_bucket, key):
            return {
                "Versions": [{"Key": key, "VersionId": "new-v1"}],
                "DeleteMarkers": [{"Key": key, "VersionId": "old-delete"}],
            }

    writer.reader = Reader()
    monkeypatch.setattr(writer, "_put", lambda *_args: Result())
    with pytest.raises(cr.ReceiptError, match="CONTROL_CONFLICT"):
        writer.ensure_bytes(BUCKET, "%s/control/test" % PREFIX, b"x")


def test_equivalent_receipt_conflict_rejects_oversize_before_get(monkeypatch):
    writer = crc.AwsCliConditionalWriter("aws")

    class Result:
        returncode = 1
        stdout = ""
        stderr = "PreconditionFailed 412"

    class Reader:
        got = False

        @staticmethod
        def list_versions(_bucket, key):
            return {"Versions": [{"Key": key, "VersionId": "v1"}]}

        @staticmethod
        def head(_bucket, _key, _version):
            return {
                "VersionId": "v1",
                "ContentLength": crc.MAX_DURABLE_RECEIPT_BYTES + 1,
                "LastModified": "2026-07-14T04:00:00Z",
            }

        def get_exact(self, *_args):
            self.got = True

    reader = Reader()
    writer.reader = reader
    monkeypatch.setattr(writer, "_put", lambda *_args: Result())
    with pytest.raises(cr.ReceiptError, match="allowed bound"):
        writer.ensure_equivalent_bytes(
            BUCKET, "%s/control/receipt.json" % PREFIX, b"{}\n",
            lambda _stored: True)
    assert reader.got is False


def test_control_writer_source_has_no_large_copy_delete_tag_or_lifecycle_surface():
    source = inspect.getsource(crc).lower().replace("_", "-")
    assert "put-object" in source
    for forbidden in (
        "copy-object", "create-multipart-upload", "upload-part",
        "put-object-tagging", "put-object-version-tagging", "delete-object",
        "delete-objects", "put-bucket-lifecycle", "put-object-legal-hold",
    ):
        assert forbidden not in source
