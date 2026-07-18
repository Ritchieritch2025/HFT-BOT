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
import threading
import tracemalloc

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import canonical_receipts as cr  # noqa: E402
import canonical_receipt_control as crc  # noqa: E402
import forward_canonical_receipts as fcr  # noqa: E402
import publication_generation as pg  # noqa: E402
import warehouse_common as wc  # noqa: E402


DATE = "2026-07-13"
NEXT_DATE = "2026-07-14"
BUCKET = "kalshi-vault-fixture"
PREFIX = "ec2"
HISTORICAL_DATE = "2026-07-10"


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


def _historical_metadata_tree(tmp_path, date=HISTORICAL_DATE):
    """A full-v2 day whose raw directory deliberately does not exist."""
    raw_root = tmp_path / "pruned-raw"
    warehouse = tmp_path / "warehouse"
    facts = warehouse / "facts"
    quality = tmp_path / "event_packs"
    quality.mkdir(parents=True)

    manifest_rows = []
    archive_entries = []
    for table, ext, payload in (
            ("orderbooks_l1", "parquet", b"historical-l1"),
            ("orderbooks_full", "parquet", b"historical-l2"),
            ("trades", "csv.gz", b"historical-trades")):
        name = wc.partition_file(table, "Sports", "Baseball", date, ext)
        rel = ("%s/category=Sports/subcategory=Baseball/date=%s/%s"
               % (table, date, name))
        path = facts / rel
        _write(path, payload)
        stat = path.stat()
        archive_entries.append({
            "file": rel,
            "table": table,
            "size": len(payload),
            "sha256": _sha(payload),
            "md5": hashlib.md5(payload).hexdigest(),  # noqa: S324
            "inode": stat.st_ino,
            "mtime_ns": stat.st_mtime_ns,
            "ctime_ns": stat.st_ctime_ns,
        })
        manifest_rows.append([
            date, table, "Sports", "Baseball", 1, str(path),
            hashlib.md5(payload).hexdigest(),  # noqa: S324
            "2026-07-11T02:00:00Z",
        ])

    manifest = warehouse / "manifest.csv"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(cr.MANIFEST_FIELDS)
        writer.writerows(manifest_rows)
    manifest_sha, _rows = wc.manifest_date_sha256(str(manifest), date)

    firehose_payload = b"historical-firehose-not-retained"
    l2_payload = b"historical-l2-not-retained"
    rfq_payload = b"historical-rfq-not-retained"
    raw_entries = [
        _seal_file_entry(
            "date=%s/firehose_00.ndjson" % date, firehose_payload),
        _seal_file_entry("date=%s/l2_00.ndjson" % date, l2_payload),
        _seal_file_entry("date=%s/rfq_00.ndjson" % date, rfq_payload),
    ]
    capture = {
        "schema_version": "capture-gap-scan-receipt-v1",
        "date": date,
        "files": [{
            "file": raw_entries[0]["file"],
            "bytes": raw_entries[0]["size"],
        }],
        "n_files": 1,
        "total_bytes": raw_entries[0]["size"],
        "records": 1,
        "unparsed": 0,
        "unreadable": False,
        "gaps": [],
    }
    _write(
        quality / ("capture_gap_receipt_%s.json" % date),
        json.dumps(capture, sort_keys=True) + "\n")
    l2 = {
        "schema_version": "l2-gap-receipt-v1",
        "date": date,
        "file_inventory": [{
            "file": raw_entries[1]["file"],
            "bytes": raw_entries[1]["size"],
        }],
        "no_l2_files": False,
        "lines": 1,
        "parse_errors": 0,
        "seq_gap_events": 0,
        "seq_missed_total": 0,
        "sids_total": 1,
        "sids_with_seq_gaps": 0,
    }
    _write(
        quality / ("l2_gaps_%s.json" % date),
        json.dumps(l2, sort_keys=True) + "\n")
    seal = {
        "version": 2,
        "method": "full_v2",
        "status": "SEALED",
        "date": date,
        "sealed_at": "2026-07-11T02:00:00Z",
        "manifest_date_sha256": manifest_sha,
        "archive_files": len(archive_entries),
        "archive_rows": len(manifest_rows),
        "archive_file_stats": archive_entries,
        "raw_files": raw_entries,
    }
    _write(
        warehouse / "seals" / ("date=%s.json" % date),
        json.dumps(seal, sort_keys=True, indent=2) + "\n")
    return {
        "date": date,
        "raw_root": raw_root,
        "warehouse": warehouse,
        "quality": quality,
        "facts": facts,
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


def _bounded_worker_inventory(count=4):
    payloads = {
        "ec2/worker-object-%d.bin" % index:
        ("payload-%d" % index).encode("ascii")
        for index in range(count)
    }
    objects = [{
        "bucket": BUCKET,
        "key": key,
        "size": len(payload),
        "sha256": _sha(payload),
        "family": "worker_fixture",
        "canonical_source": "EXISTING_EXACT_VERSION",
        "_expected_version_id": "v1",
    } for key, payload in payloads.items()]
    return objects, payloads


class CoordinatedVerificationClient:
    """Force reverse completion while recording bounded GET concurrency."""

    def __init__(self, payloads, *, coordinate=False, faults=None):
        self.payloads = payloads
        self.coordinate = coordinate
        self.faults = faults or {}
        self.barrier = threading.Barrier(len(payloads)) if coordinate else None
        self.done = [threading.Event() for _payload in payloads]
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.get_threads = set()
        self.dest_parents = []
        self.completion_order = []

    @staticmethod
    def _index(key):
        return int(key.rsplit("-", 1)[1].removesuffix(".bin"))

    def head(self, _bucket, key, version_id=None):
        return {
            "VersionId": version_id or "v1",
            "ContentLength": len(self.payloads[key]),
            "LastModified": "2026-07-14T02:30:00Z",
        }

    def get_exact(self, _bucket, key, _version_id, dest):
        index = self._index(key)
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.get_threads.add(threading.get_ident())
            self.dest_parents.append(Path(dest).parent)
        try:
            if self.barrier is not None:
                self.barrier.wait(timeout=5)
                if index + 1 < len(self.done):
                    assert self.done[index + 1].wait(timeout=5)
            fault = self.faults.get(key)
            if fault == "exception":
                raise RuntimeError("fixture adapter failure for %s" % key)
            payload = self.payloads[key]
            if fault == "corrupt":
                payload = b"!" * len(payload)
            Path(dest).write_bytes(payload)
            with self.lock:
                self.completion_order.append(key)
        finally:
            self.done[index].set()
            with self.lock:
                self.active -= 1


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

    def counted_read(self, rel, max_bytes=None):
        calls.append(rel)
        return real_read(self, rel, max_bytes=max_bytes)

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

    def replace_after_read(self, rel, max_bytes=None):
        result = real_read(self, rel, max_bytes=max_bytes)
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


@pytest.mark.parametrize("workers", [None, True, 0, -1, 5, 4.0, "4"])
def test_verify_inventory_rejects_invalid_or_unbounded_workers(
        tmp_path, workers):
    class NoCallsClient:
        def head(self, *_args, **_kwargs):
            pytest.fail("invalid workers must fail before any client call")

    temp_root = tmp_path / "must-not-be-created"
    with pytest.raises(cr.ReceiptError, match="VERIFY_WORKERS_INVALID"):
        cr.verify_inventory(
            [], NoCallsClient(), str(temp_root), workers=workers)
    assert not temp_root.exists()


def test_verify_inventory_default_is_sequential_but_four_workers_are_bounded(
        tmp_path):
    objects, payloads = _bounded_worker_inventory()
    sequential_client = CoordinatedVerificationClient(payloads)
    sequential, failures, complete = cr.verify_inventory(
        objects, sequential_client, str(tmp_path / "sequential"))
    assert complete and failures == []
    assert sequential_client.max_active == 1
    assert sequential_client.get_threads == {threading.get_ident()}

    parallel_root = tmp_path / "parallel"
    parallel_client = CoordinatedVerificationClient(
        payloads, coordinate=True)
    parallel, failures, complete = cr.verify_inventory(
        objects, parallel_client, str(parallel_root), workers=4)

    keys = [row["key"] for row in objects]
    assert complete and failures == []
    assert parallel_client.max_active == cr.MAX_VERIFY_WORKERS == 4
    assert parallel_client.completion_order == list(reversed(keys))
    assert [row["key"] for row in parallel] == keys
    assert parallel == sequential
    assert len(set(parallel_client.dest_parents)) == len(objects)
    assert list(parallel_root.iterdir()) == []
    assert all(row["_inventory_complete"] is True
               and row["_inventory_total"] == len(objects)
               for row in parallel)


def test_metadata_only_head_preflight_runs_with_four_bounded_workers(tmp_path):
    objects, payloads = _bounded_worker_inventory()

    class CoordinatedHeadClient:
        def __init__(self):
            self.barrier = threading.Barrier(len(objects))
            self.lock = threading.Lock()
            self.active = 0
            self.max_active = 0

        def head(self, _bucket, key, version_id=None):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                self.barrier.wait(timeout=5)
                return {
                    "VersionId": version_id or "v1",
                    "ContentLength": len(payloads[key]),
                    "LastModified": "2026-07-14T02:30:00Z",
                }
            finally:
                with self.lock:
                    self.active -= 1

        def get_exact(self, *_args, **_kwargs):
            pytest.fail("metadata-only verification must never GET")

    client = CoordinatedHeadClient()
    temp_root = tmp_path / "metadata-parallel"
    verified, failures, complete = cr.verify_inventory(
        objects, client, str(temp_root), metadata_only=True, workers=4)

    assert failures == [] and complete is False
    assert client.max_active == 4
    assert [row["key"] for row in verified] == [row["key"] for row in objects]
    assert all(row["verification_state"] == "METADATA_ONLY"
               and row["_inventory_complete"] is False
               for row in verified)
    assert list(temp_root.iterdir()) == []


def test_parallel_verify_collects_failures_in_inventory_order_and_cleans_temp(
        tmp_path):
    objects, payloads = _bounded_worker_inventory()
    keys = [row["key"] for row in objects]
    faults = {
        keys[1]: "corrupt",
        keys[3]: "exception",
    }
    sequential_root = tmp_path / "sequential-failures"
    sequential_client = CoordinatedVerificationClient(
        payloads, faults=faults)
    expected = cr.verify_inventory(
        objects, sequential_client, str(sequential_root))
    client = CoordinatedVerificationClient(
        payloads, coordinate=True, faults=faults)
    temp_root = tmp_path / "parallel-failures"
    verified, failures, complete = cr.verify_inventory(
        objects, client, str(temp_root), workers=4)

    assert (verified, failures, complete) == expected
    assert complete is False
    assert client.max_active == 4
    assert [row["key"] for row in verified] == [keys[0], keys[2]]
    assert [(row["key"], row["code"]) for row in failures] == [
        (keys[1], "SHA256_MISMATCH"),
        (keys[3], "UNEXPECTED_ERROR"),
    ]
    assert all(row["_inventory_complete"] is False
               and row["_inventory_total"] == len(objects)
               for row in verified)
    assert len(set(client.dest_parents)) == len(objects)
    assert list(sequential_root.iterdir()) == []
    assert list(temp_root.iterdir()) == []


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


def test_forward_cli_metadata_preflight_is_rc0_without_receipt(
        receipt_tree, tmp_path, monkeypatch, capsys):
    """Pin the deployed CLI contract consumed by the daily orchestrator."""
    seal_binding, objects = _build(receipt_tree)
    client = FakeExactVersionClient(receipt_tree["payloads"])
    monkeypatch.setattr(
        fcr, "build_forward_inventory",
        lambda *_args, **_kwargs: (seal_binding, objects))
    monkeypatch.setattr(cr, "AwsCliS3Client", lambda _aws: client)
    monkeypatch.setattr(wc, "load_config", lambda: {
        "raw_root": str(receipt_tree["raw_root"]),
        "warehouse_root": str(receipt_tree["warehouse"]),
    })
    output = tmp_path / "metadata-preflight"
    rc = fcr.main([
        "shadow-forward", "--date", DATE, "--bucket", BUCKET,
        "--prefix", PREFIX, "--aux-bundle", str(tmp_path / "unused-aux"),
        "--version-binding", str(tmp_path / "unused-version-binding"),
        "--aws-cli", "/fixture/aws", "--metadata-only",
        "--output-root", str(output),
    ])
    assert rc == 0
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    expected_status = output / ("date=" + DATE) / "SHADOW_STATUS.json"
    assert result == {
        "failures": 0,
        "receipt": None,
        "state": "METADATA_PREFLIGHT_VERIFIED",
        "status": str(expected_status),
    }
    status = json.loads(expected_status.read_text())
    assert status["state"] == "METADATA_PREFLIGHT_VERIFIED"
    assert status["metadata_only"] is True
    assert status["content_verification_complete"] is False
    assert status["complete"] is False
    assert status["failures"] == []
    assert not any(op[0] == "get_exact" for op in client.ops)


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
    _write_fixture_forward_binding(
        tree, Path(path), Path(root) / "version-bindings")
    payloads = dict(tree["payloads"])
    for row in descriptor["objects"]:
        if row["local_relpath"] is not None:
            payloads[row["key"]] = (
                Path(path) / row["local_relpath"]
            ).read_bytes()
    return Path(path), descriptor, payloads


def _write_fixture_forward_binding(tree, bundle, output_root):
    loaded_descriptor, rows = fcr.load_forward_auxiliary_set(
        str(bundle), DATE, BUCKET, PREFIX, tree["seal"],
        cr.sha256_file(str(tree["seal_path"])))
    catalog_files = []
    for rel in cr.CATALOG_REQUIRED + cr.CATALOG_OPTIONAL:
        key = "%s/warehouse/catalog/%s" % (PREFIX, rel)
        payload = tree["payloads"].get(key)
        if payload is not None:
            catalog_files.append({
                "relative_path": "catalog/" + rel,
                "size": len(payload), "sha256": _sha(payload),
            })
    catalog_generation = pg.build_manifest("catalog", catalog_files)
    dim_files = []
    for name in cr.DIM_REQUIRED:
        key = "%s/warehouse/dim/snapshots/date=%s/%s" % (
            PREFIX, DATE, name)
        payload = tree["payloads"][key]
        dim_files.append({
            "relative_path": "dim/snapshots/date=%s/%s" % (DATE, name),
            "size": len(payload), "sha256": _sha(payload),
        })
    dim_generation = pg.build_manifest(
        "dim", dim_files, date=DATE,
        source_catalog_generation_id=catalog_generation["generation_id"])
    member_objects = []
    for row in catalog_generation["files"] + dim_generation["files"]:
        logical = "warehouse/" + row["relative_path"]
        member_objects.append({
            "logical_source_key": logical,
            "bucket": BUCKET, "key": PREFIX + "/" + logical,
            "VersionId": "v1", "size": row["size"],
            "sha256": row["sha256"],
            "LastModified": "2026-07-14T02:30:00Z",
        })
    member_objects.sort(key=lambda row: row["logical_source_key"])
    seal_raw = tree["seal_path"].read_bytes()
    exact_seal = {
        "bucket": BUCKET,
        "key": "%s/warehouse/seals/date=%s.json" % (PREFIX, DATE),
        "VersionId": "v1", "size": len(seal_raw),
        "sha256": _sha(seal_raw),
        "LastModified": "2026-07-14T02:30:00Z",
    }
    witness = fcr.build_generation_witness_payload(
        DATE, BUCKET, PREFIX, exact_seal, catalog_generation,
        dim_generation, member_objects)
    witness_key, witness_raw = fcr.generation_witness_artifact(witness)
    tree["payloads"][witness_key] = witness_raw
    witness_object = {
        "bucket": BUCKET, "key": witness_key, "VersionId": "v1",
        "size": len(witness_raw), "sha256": _sha(witness_raw),
        "LastModified": "2026-07-14T02:30:00Z",
    }
    resolutions = []
    for row in fcr.forward_version_resolution_targets(rows):
        evidence = {
            "logical_source_key": row["logical_source_key"],
            "key": row["key"], "current_version_id": "v1",
            "current_exact_sha256_match": True,
            "history_pages": 0, "versions_seen": 1,
            "same_size_candidates": 1, "exact_gets": 1,
            "matching_version_ids": ["v1"],
            "selected_version_id": "v1",
            "selection_rule": fcr.FORWARD_VERSION_SELECTION_RULE,
        }
        resolutions.append({
            "logical_source_key": row["logical_source_key"],
            "bucket": row["bucket"], "key": row["key"],
            "size": row["size"], "sha256": row["sha256"],
            "VersionId": "v1", "LastModified": "2026-07-14T02:30:00Z",
            "resolution": "CURRENT_EXACT_SHA256_MATCH",
            "resolver_evidence": evidence,
        })
    path, _payload = fcr.write_forward_version_binding(
        str(output_root), loaded_descriptor, rows, DATE, BUCKET, PREFIX,
        witness, witness_object, resolutions)
    return Path(path)


def _forward_binding(bundle):
    matches = list((bundle.parent.parent / "version-bindings").glob(
        "date=*/VERSION-BINDING-*.json"))
    assert len(matches) == 1
    return matches[0]


def _forward_client(payloads, **kwargs):
    client = FakeExactVersionClient(payloads, **kwargs)
    return client


def _write_rehashed_forward_binding(output_dir, payload):
    payload = copy.deepcopy(payload)
    evidence = {row["logical_source_key"]: row
                for row in payload["resolver_evidence"]}
    for row in payload["objects"]:
        row["resolver_evidence_sha256"] = cr.canonical_sha256(
            evidence[row["logical_source_key"]])
    payload["object_set_sha256"] = cr.canonical_sha256(payload["objects"])
    payload["resolver_evidence_set_sha256"] = cr.canonical_sha256(
        payload["resolver_evidence"])
    projection = {key: value for key, value in payload.items()
                  if key != "version_binding_sha256"}
    digest = cr.canonical_sha256(projection)
    payload["version_binding_sha256"] = digest
    path = Path(output_dir) / ("VERSION-BINDING-%s.json" % digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
    return path


def test_forward_aux_is_deterministic_and_copies_no_catalog_or_dim_bytes(
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
    dims = [row for row in descriptor["objects"]
            if row["family"] == "dim_snapshot"]
    assert catalog == [] and dims == []
    assert descriptor["generation_witness_contract"] == \
        fcr._generation_witness_contract({
            "sha256": cr.sha256_file(str(receipt_tree["seal_path"])),
            "sealed_at": receipt_tree["seal"]["sealed_at"],
        }, DATE, BUCKET, PREFIX)
    assert descriptor["generation_witness_contract"]["key_prefix"] == \
        "%s/control/publication-generations/v1/date=%s/" % (PREFIX, DATE)
    local_files = {path.relative_to(first).as_posix()
                   for path in first.rglob("*") if path.is_file()}
    assert not any(path.startswith("publication-snapshot/")
                   for path in local_files)
    assert not any(row["family"] == "catalog_cutoff_evidence"
                   for row in descriptor["objects"])


def test_forward_large_correction_is_streamed_and_never_copied_or_retained(
        receipt_tree, tmp_path, monkeypatch):
    late_path = (receipt_tree["warehouse"] / "corrections"
                 / ("date=%s" % DATE) / "late_rows.ndjson")
    line = late_path.read_bytes()
    repeats = (16 * 1024 * 1024 // len(line)) + 1
    with late_path.open("wb") as handle:
        for _index in range(repeats):
            handle.write(line)
    ledger_path = (receipt_tree["warehouse"] / "corrections"
                   / "ledger.ndjson")
    ledger = [json.loads(raw) for raw in ledger_path.read_text().splitlines()]
    ledger[0]["n_rows"] = repeats
    ledger_path.write_text("".join(
        json.dumps(row, sort_keys=True) + "\n" for row in ledger))

    original_freeze = cr._freeze_file

    def reject_unbounded_correction_read(path):
        if "%scorrections%s" % (os.sep, os.sep) in os.fspath(path):
            pytest.fail("large correction/ledger must use the streaming path")
        return original_freeze(path)

    monkeypatch.setattr(cr, "_freeze_file", reject_unbounded_correction_read)
    tracemalloc.start()
    bundle, descriptor = fcr.freeze_forward_auxiliary_set(
        DATE, BUCKET, PREFIX, str(receipt_tree["raw_root"]),
        str(receipt_tree["warehouse"]), str(receipt_tree["quality"]),
        str(tmp_path / "forward-large"))
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    late = next(row for row in descriptor["objects"]
                if row["source_kind"] == "correction")
    assert late["storage_mode"] == fcr.FORWARD_LARGE_ATTESTATION
    assert late["local_relpath"] is None
    assert late["size"] == late_path.stat().st_size
    assert late["sha256"] == cr.sha256_file(str(late_path))
    assert late["key"] == "%s/warehouse/corrections/date=%s/late_rows.ndjson" \
        % (PREFIX, DATE)
    assert peak < 8 * 1024 * 1024
    local_files = {
        path.relative_to(bundle).as_posix()
        for path in Path(bundle).rglob("*") if path.is_file()
    }
    assert "corrections/late_rows.ndjson" not in local_files
    loaded_descriptor, loaded = fcr.load_forward_auxiliary_set(
        bundle, DATE, BUCKET, PREFIX, receipt_tree["seal"],
        cr.sha256_file(str(receipt_tree["seal_path"])))
    assert loaded_descriptor == descriptor
    loaded_late = next(row for row in loaded
                       if row["source_kind"] == "correction")
    assert "_local_payload" not in loaded_late
    assert "_local_path" not in loaded_late


def test_forward_inventory_defers_rfq_by_default_but_preserves_capability(
        receipt_tree, tmp_path):
    bundle, _descriptor, _payloads = _forward_bundle(
        receipt_tree, tmp_path / "forward")
    seal_binding, objects = fcr.build_forward_inventory(
        DATE, BUCKET, PREFIX, str(receipt_tree["raw_root"]),
        str(receipt_tree["warehouse"]), str(receipt_tree["quality"]),
        str(bundle), str(_forward_binding(bundle)))
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
        str(bundle), str(_forward_binding(bundle)),
        include_rfq_durability=True)
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
    assert catalog and all(row["_expected_version_id"] == "v1"
                           for row in catalog)


def test_forward_inventory_uses_bound_versions_after_local_sources_change(
        receipt_tree, tmp_path):
    bundle, _descriptor, _payloads = _forward_bundle(
        receipt_tree, tmp_path / "forward")
    binding_path = _forward_binding(bundle)
    binding_payload = json.loads(binding_path.read_text())
    frozen = {
        row["logical_source_key"]: (row["size"], row["sha256"])
        for row in binding_payload["objects"]
        if row["family"] in {"catalog_at_publication", "dim_snapshot",
                             "corrections_at_cutoff"}
    }
    catalog_path = (receipt_tree["warehouse"] / "catalog" / "series"
                    / "part-00000.parquet")
    catalog_path.write_bytes(b"producer-new-catalog-generation")
    dim_path = (receipt_tree["warehouse"] / "dim" / "snapshots"
                / ("date=%s" % DATE) / "series.csv")
    dim_path.write_bytes(b"id,value\nseries,producer-new\n")
    late_path = (receipt_tree["warehouse"] / "corrections"
                 / ("date=%s" % DATE) / "late_rows.ndjson")
    with late_path.open("ab") as handle:
        handle.write(late_path.read_bytes().splitlines()[0] + b"\n")

    _binding, objects = fcr.build_forward_inventory(
        DATE, BUCKET, PREFIX, str(receipt_tree["raw_root"]),
        str(receipt_tree["warehouse"]), str(receipt_tree["quality"]),
        str(bundle), str(_forward_binding(bundle)))
    observed = {
        row["logical_source_key"]: (row["size"], row["sha256"])
        for row in objects if row["logical_source_key"] in frozen
    }
    assert observed == frozen
    witness_objects = [row for row in objects if row["family"] in {
        "catalog_at_publication", "dim_snapshot"}]
    assert witness_objects
    assert all("_local_path" not in row and "_local_payload" not in row
               and row["canonical_source"]
               == "EXACT_GENERATION_WITNESS_MEMBER"
               for row in witness_objects)


def test_forward_binding_rejects_witness_member_not_bound_by_manifest(
        receipt_tree, tmp_path):
    bundle, _descriptor, _payloads = _forward_bundle(
        receipt_tree, tmp_path / "forward")
    payload = json.loads(_forward_binding(bundle).read_text())
    witness = payload["generation_witness"]
    witness["objects"][0]["sha256"] = "0" * 64
    witness["object_set_sha256"] = cr.canonical_sha256(witness["objects"])
    seal_raw = receipt_tree["seal_path"].read_bytes()
    with pytest.raises(cr.ReceiptError, match="witness member mismatch"):
        fcr.validate_generation_witness(
            witness, DATE, BUCKET, PREFIX, _sha(seal_raw), len(seal_raw),
            receipt_tree["seal"]["sealed_at"])


@pytest.mark.parametrize("link_kind", ["member", "root"])
def test_forward_freeze_does_not_read_local_catalog_symlinks(
        receipt_tree, tmp_path, link_kind):
    catalog = receipt_tree["warehouse"] / "catalog"
    if link_kind == "member":
        member = catalog / "series" / "part-00000.parquet"
        payload = member.read_bytes()
        member.unlink()
        outside = tmp_path / "outside.parquet"
        outside.write_bytes(payload)
        member.symlink_to(outside)
    else:
        moved = receipt_tree["warehouse"] / "catalog-real"
        catalog.rename(moved)
        catalog.symlink_to(moved, target_is_directory=True)
    path, descriptor = fcr.freeze_forward_auxiliary_set(
        DATE, BUCKET, PREFIX, str(receipt_tree["raw_root"]),
        str(receipt_tree["warehouse"]), str(receipt_tree["quality"]),
        str(tmp_path / ("symlink-" + link_kind)))
    assert Path(path).is_dir()
    assert not any(row["family"] in {
        "catalog_at_publication", "dim_snapshot"}
        for row in descriptor["objects"])


def test_forward_binding_requires_every_generation_witness_member(
        receipt_tree, tmp_path):
    bundle, _descriptor, _payloads = _forward_bundle(
        receipt_tree, tmp_path / "forward")
    payload = json.loads(_forward_binding(bundle).read_text())
    witness = payload["generation_witness"]
    witness["objects"].pop()
    witness["object_set_sha256"] = cr.canonical_sha256(witness["objects"])
    seal_raw = receipt_tree["seal_path"].read_bytes()
    with pytest.raises(cr.ReceiptError, match="witness member set incomplete"):
        fcr.validate_generation_witness(
            witness, DATE, BUCKET, PREFIX, _sha(seal_raw), len(seal_raw),
            receipt_tree["seal"]["sealed_at"])


@pytest.mark.parametrize("field", ["size", "sha256", "evidence_binding"])
def test_forward_inventory_rejects_forged_large_correction_attestation(
        receipt_tree, tmp_path, field):
    bundle, descriptor, payloads = _forward_bundle(
        receipt_tree, tmp_path / ("forward-%s" % field))
    tampered = copy.deepcopy(descriptor)
    late = next(row for row in tampered["objects"]
                if row["source_kind"] == "correction")
    if field == "size":
        late[field] += 1
    elif field == "sha256":
        late[field] = "0" * 64
    else:
        late[field]["late_rows_validation"]["row_count"] += 1
    desired = [{
        key: row[key] for key in (
            "bucket", "key", "logical_source_key", "source_kind", "family",
            "date", "size", "sha256", "seal_binding", "evidence_binding",
            "required", "durability_scope", "research_candidate",
            "exposure_policy", "version_resolution", "canonical_source")
    } for row in tampered["objects"]]
    desired.sort(key=lambda row: (
        row["logical_source_key"], row["bucket"], row["key"]))
    tampered["desired_set_sha256"] = cr.canonical_sha256(desired)
    projection = {key: value for key, value in tampered.items()
                  if key != "aux_set_sha256"}
    tampered["aux_set_sha256"] = cr.canonical_sha256(projection)
    (bundle / "AUX_SET.json").write_text(
        json.dumps(tampered, sort_keys=True, indent=2) + "\n")
    renamed = bundle.with_name("aux-set=%s" % tampered["aux_set_sha256"])
    bundle.rename(renamed)
    version_binding = (_forward_binding(renamed)
                       if field == "evidence_binding" else
                       _write_fixture_forward_binding(
                           receipt_tree, renamed,
                           tmp_path / ("forged-binding-" + field)))
    if field == "evidence_binding":
        with pytest.raises(cr.ReceiptError):
            fcr.build_forward_inventory(
                DATE, BUCKET, PREFIX, str(receipt_tree["raw_root"]),
                str(receipt_tree["warehouse"]), str(receipt_tree["quality"]),
                str(renamed), str(version_binding))
    else:
        _binding, objects = fcr.build_forward_inventory(
            DATE, BUCKET, PREFIX, str(receipt_tree["raw_root"]),
            str(receipt_tree["warehouse"]), str(receipt_tree["quality"]),
            str(renamed), str(version_binding))
        _verified, failures, complete = cr.verify_inventory(
            objects, _forward_client(payloads), str(tmp_path / "exact"))
        assert not complete
        assert any(row["family"] == "corrections_at_cutoff"
                   and row["code"] in {"SIZE_MISMATCH", "SHA256_MISMATCH"}
                   for row in failures)


def test_forward_loader_rejects_legacy_large_correction_before_read(
        receipt_tree, tmp_path, monkeypatch):
    bundle, descriptor, _payloads = _forward_bundle(
        receipt_tree, tmp_path / "forward-legacy-large")
    tampered = copy.deepcopy(descriptor)
    late = next(row for row in tampered["objects"]
                if row["source_kind"] == "correction")
    late.update({
        "storage_mode": "LOCAL_FROZEN_BACKFILL",
        "local_relpath": "corrections/late_rows.ndjson",
        "key": ("%s/warehouse/publication-snapshots/v1/date=%s/"
                "corrections/late_rows/sha256=%s/late_rows.ndjson") %
               (PREFIX, DATE, late["sha256"]),
        "version_resolution": "WRITE_ONCE_CONTENT_ADDRESSED_EXACT",
        "canonical_source": "PLANNED_DATE_SCOPED_CONTROL_SYNC",
    })
    large = bundle / late["local_relpath"]
    large.parent.mkdir(parents=True, exist_ok=True)
    with large.open("xb") as handle:
        handle.truncate(256 * 1024 * 1024)
    late["size"] = large.stat().st_size
    desired = [{
        key: row[key] for key in (
            "bucket", "key", "logical_source_key", "source_kind", "family",
            "date", "size", "sha256", "seal_binding", "evidence_binding",
            "required", "durability_scope", "research_candidate",
            "exposure_policy", "version_resolution", "canonical_source")
    } for row in tampered["objects"]]
    desired.sort(key=lambda row: (
        row["logical_source_key"], row["bucket"], row["key"]))
    tampered["desired_set_sha256"] = cr.canonical_sha256(desired)
    projection = {key: value for key, value in tampered.items()
                  if key != "aux_set_sha256"}
    tampered["aux_set_sha256"] = cr.canonical_sha256(projection)
    (bundle / "AUX_SET.json").write_text(
        json.dumps(tampered, sort_keys=True, indent=2) + "\n")
    renamed = bundle.with_name("aux-set=%s" % tampered["aux_set_sha256"])
    bundle.rename(renamed)

    original_read = cr._BundleReader.read

    def forbid_large_read(self, rel, max_bytes=None):
        if rel == "corrections/late_rows.ndjson":
            pytest.fail("legacy large correction was read before rejection")
        return original_read(self, rel, max_bytes=max_bytes)

    monkeypatch.setattr(cr._BundleReader, "read", forbid_large_read)
    with pytest.raises(cr.ReceiptError, match="large canonical attestation"):
        fcr.load_forward_auxiliary_set(
            str(renamed), DATE, BUCKET, PREFIX, receipt_tree["seal"],
            cr.sha256_file(str(receipt_tree["seal_path"])))
    with pytest.raises(cr.ReceiptError, match="CONTROL_KEY_FORBIDDEN"):
        fcr.expected_control_key(late, PREFIX, DATE)


def test_legacy_freeze_rejects_large_correction_before_materializing(
        receipt_tree, tmp_path):
    late_path = (receipt_tree["warehouse"] / "corrections"
                 / ("date=%s" % DATE) / "late_rows.ndjson")
    with late_path.open("r+b") as handle:
        handle.truncate(cr.MAX_DATE_CONTROL_BYTES + 1)
    with pytest.raises(
            cr.ReceiptError, match="LEGACY_AUX_CONTROL_TOO_LARGE.*forward"):
        _freeze_again(receipt_tree, tmp_path / "legacy-large")


def test_bounded_freeze_rejects_append_after_pinned_open(tmp_path,
                                                         monkeypatch):
    source = tmp_path / "growing-control.bin"
    source.write_bytes(b"a" * 512)
    original_read = cr.os.read
    appended = False

    def append_during_read(fd, size):
        nonlocal appended
        chunk = original_read(fd, size)
        if chunk and not appended:
            appended = True
            with source.open("ab") as handle:
                handle.write(b"b" * 1024)
        return chunk

    monkeypatch.setattr(cr.os, "read", append_during_read)
    with pytest.raises(cr.ReceiptError, match="CONTROL_TOO_LARGE"):
        cr._freeze_file(str(source), max_bytes=1024)


def test_forward_full_verification_captures_version_and_last_modified(
        receipt_tree, tmp_path):
    bundle, _descriptor, payloads = _forward_bundle(
        receipt_tree, tmp_path / "forward")
    seal_binding, objects = fcr.build_forward_inventory(
        DATE, BUCKET, PREFIX, str(receipt_tree["raw_root"]),
        str(receipt_tree["warehouse"]), str(receipt_tree["quality"]),
        str(bundle), str(_forward_binding(bundle)))
    client = _forward_client(payloads)
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
        str(bundle), str(_forward_binding(bundle)))
    bad_key = objects[0]["key"]
    client = _forward_client(
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


def test_forward_version_binding_rejects_catalog_digest_tamper(
        receipt_tree, tmp_path):
    bundle, _descriptor, _payloads = _forward_bundle(
        receipt_tree, tmp_path / "forward")
    descriptor, rows = fcr.load_forward_auxiliary_set(
        str(bundle), DATE, BUCKET, PREFIX, receipt_tree["seal"],
        cr.sha256_file(str(receipt_tree["seal_path"])))
    path = _forward_binding(bundle)
    tampered = json.loads(path.read_text())
    row = next(item for item in tampered["objects"]
               if item["family"] == "catalog_at_publication")
    row["sha256"] = "0" * 64
    path.write_text(json.dumps(tampered, sort_keys=True, indent=2) + "\n")
    with pytest.raises(cr.ReceiptError, match="binding digest mismatch"):
        fcr.load_forward_version_binding(
            str(path), descriptor, rows, DATE, BUCKET, PREFIX)


def test_forward_version_binding_rejects_catalog_table_semantic_tamper(
        receipt_tree, tmp_path):
    bundle, _descriptor, _payloads = _forward_bundle(
        receipt_tree, tmp_path / "forward")
    descriptor, rows = fcr.load_forward_auxiliary_set(
        str(bundle), DATE, BUCKET, PREFIX, receipt_tree["seal"],
        cr.sha256_file(str(receipt_tree["seal_path"])))
    path = _forward_binding(bundle)
    tampered = json.loads(path.read_text())
    row = next(item for item in tampered["objects"]
               if item["family"] == "catalog_at_publication")
    row["table"] = "forged_table"
    path.write_text(json.dumps(tampered, sort_keys=True, indent=2) + "\n")
    with pytest.raises(cr.ReceiptError, match="binding digest mismatch"):
        fcr.load_forward_version_binding(
            str(path), descriptor, rows, DATE, BUCKET, PREFIX)


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
        str(tree["warehouse"]), str(tree["quality"]), str(bundle),
        str(_forward_binding(bundle)))
    client = _forward_client(payloads)
    verified, failures, complete = cr.verify_inventory(
        objects, client, str(tmp_path / "exact"))
    assert complete and failures == []
    path = cr.write_shadow_receipt(
        str(tmp_path / "shadow"), DATE, seal_binding, verified,
        "2026-07-14T04:00:00Z", "b" * 40)
    return bundle, Path(path), seal_binding, verified


@pytest.mark.parametrize(("worker_args", "expected_workers"), [
    ([], 1),
    (["--workers", "4"], 4),
])
def test_shadow_forward_cli_defaults_to_one_and_propagates_explicit_workers(
        tmp_path, monkeypatch, capsys, worker_args, expected_workers):
    inventory = [{"key": "fixture", "size": 1}]
    verified = [{"key": "fixture", "durability_verified": False}]
    observed = {}
    monkeypatch.setattr(
        fcr.wc, "load_config", lambda: {
            "raw_root": str(tmp_path / "raw"),
            "warehouse_root": str(tmp_path / "warehouse"),
        })
    monkeypatch.setattr(
        fcr, "build_forward_inventory",
        lambda *_args, **_kwargs: ({"date": DATE}, inventory))
    monkeypatch.setattr(
        fcr.cr, "AwsCliS3Client", lambda _aws: object())

    def fake_verify(rows, _client, _temp_root, **kwargs):
        observed["inventory"] = rows
        observed.update(kwargs)
        return verified, [], False

    monkeypatch.setattr(fcr.cr, "verify_inventory", fake_verify)
    output = tmp_path / ("shadow-%d" % expected_workers)
    assert fcr.main([
        "shadow-forward", "--date", DATE,
        "--aux-bundle", str(tmp_path / "aux"),
        "--version-binding", str(tmp_path / "binding.json"),
        "--metadata-only", "--output-root", str(output),
        *worker_args,
    ]) == 0
    assert observed == {
        "inventory": inventory,
        "metadata_only": True,
        "probe_limit": None,
        "workers": expected_workers,
    }
    assert json.loads(capsys.readouterr().out)["state"] == \
        "METADATA_PREFLIGHT_VERIFIED"


@pytest.mark.parametrize("workers", ["-1", "0", "5", "not-an-integer"])
def test_shadow_forward_cli_rejects_invalid_workers_before_work(
        tmp_path, monkeypatch, workers):
    monkeypatch.setattr(
        fcr.wc, "load_config",
        lambda: pytest.fail("invalid workers must fail during argument parsing"))
    with pytest.raises(SystemExit) as excinfo:
        fcr.main([
            "shadow-forward", "--date", DATE,
            "--aux-bundle", str(tmp_path / "aux"),
            "--version-binding", str(tmp_path / "binding.json"),
            "--workers", workers,
        ])
    assert excinfo.value.code == 2


@pytest.mark.parametrize("workers", ["-1", "0", "5", "not-an-integer"])
def test_publish_receipt_cli_rejects_invalid_workers_before_writer_or_plan(
        tmp_path, monkeypatch, workers):
    monkeypatch.setattr(
        crc, "AwsCliConditionalWriter",
        lambda _aws: pytest.fail("invalid workers must precede writer setup"))
    monkeypatch.setattr(
        crc.wc, "load_config",
        lambda: pytest.fail("invalid workers must precede inventory planning"))
    with pytest.raises(SystemExit) as excinfo:
        crc.main([
            "publish-receipt", "--date", DATE,
            "--aux-bundle", str(tmp_path / "aux"),
            "--version-binding", str(tmp_path / "binding.json"),
            "--workers", workers, "--operator-approved",
        ])
    assert excinfo.value.code == 2


def test_publish_receipt_cli_passes_explicit_bounded_worker_count(
        tmp_path, monkeypatch, capsys):
    class Writer:
        reader = object()
        puts = 0
        reused = 0

    observed = {}
    inventory = [{"key": "fixture"}]
    verified = [{"key": "verified"}]
    monkeypatch.setattr(crc, "AwsCliConditionalWriter", lambda _aws: Writer())
    monkeypatch.setattr(
        crc.wc, "load_config", lambda: {
            "raw_root": str(tmp_path / "raw"),
            "warehouse_root": str(tmp_path / "warehouse"),
        })
    monkeypatch.setattr(
        crc.fcr, "build_forward_inventory",
        lambda *_args, **_kwargs: ({"date": DATE}, inventory))

    def fake_verify(rows, _reader, _temp_root, **kwargs):
        observed["inventory"] = rows
        observed["workers"] = kwargs.get("workers")
        return verified, [], True

    monkeypatch.setattr(crc.cr, "verify_inventory", fake_verify)
    monkeypatch.setattr(crc.cr, "_now", lambda: "2026-07-14T03:00:00Z")
    monkeypatch.setattr(crc.cr, "_code_commit", lambda: "a" * 40)
    monkeypatch.setattr(
        crc, "publish_durable_receipt",
        lambda *_args, **_kwargs: (
            tmp_path / "DURABLE-fixture.json",
            {"receipt_object": {"VersionId": "v1"}},
        ))

    assert crc.main([
        "publish-receipt", "--date", DATE,
        "--aux-bundle", str(tmp_path / "aux"),
        "--version-binding", str(tmp_path / "binding.json"),
        "--output-root", str(tmp_path / "durable"),
        "--workers", "4", "--operator-approved",
    ]) == 0
    assert observed == {"inventory": inventory, "workers": 4}
    assert json.loads(capsys.readouterr().out)["state"] == crc.DURABLE_STATE


def test_small_control_sync_uploads_only_allowlisted_retained_bytes(
        receipt_tree, tmp_path):
    bundle, _descriptor, _payloads = _forward_bundle(
        receipt_tree, tmp_path / "forward")
    writer = FakeConditionalWriter()
    path, result = crc.sync_small_controls(
        DATE, BUCKET, PREFIX, str(receipt_tree["raw_root"]),
        str(receipt_tree["warehouse"]), str(receipt_tree["quality"]),
        str(bundle), str(_forward_binding(bundle)), writer,
        str(tmp_path / "control-index"))
    assert path.is_file()
    assert result["state"] == "CANONICAL_CONTROLS_VERIFIED"
    assert result["large_data_upload_bytes"] == 0
    assert writer.calls
    keys = [key for _bucket, key, _payload in writer.calls]
    assert all("/publication-snapshots/" in key
               or "/control/quality/" in key for key in keys)
    assert not any("/facts/" in key or "/raw/" in key
                   or "/catalog/" in key or "/dim/" in key for key in keys)
    assert "%s/warehouse/corrections/date=%s/late_rows.ndjson" % (
        PREFIX, DATE) not in keys
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
            str(bundle), str(_forward_binding(bundle)), writer,
            str(tmp_path / "control-index"))
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
        quality_dir=str(receipt_tree["quality"]), aux_bundle=str(bundle),
        version_binding=str(_forward_binding(bundle)))
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
            aux_bundle=str(bundle),
            version_binding=str(_forward_binding(bundle)))
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
            aux_bundle=str(bundle),
            version_binding=str(_forward_binding(bundle)))
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
        quality_dir=str(receipt_tree["quality"]), aux_bundle=str(bundle),
        version_binding=str(_forward_binding(bundle)))
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


def test_historical_authority_accepts_pruned_raw_with_exact_receipts(tmp_path):
    tree = _historical_metadata_tree(tmp_path)
    assert not tree["raw_root"].exists()

    _seal, binding, *_rest = fcr.historical_authoritative_day_inputs(
        tree["date"], str(tree["raw_root"]), str(tree["warehouse"]),
        str(tree["quality"]))

    authority = binding["historical_metadata_authority"]
    assert authority["schema_version"] == \
        fcr.HISTORICAL_METADATA_AUTHORITY_SCHEMA
    assert authority["state"] == \
        fcr.HISTORICAL_METADATA_AUTHORITY_STATE
    assert authority["local_raw_bytes_read"] == 0
    assert authority["raw_rebuild_or_download"] is False
    assert authority["capture_receipt"]["sha256"]
    assert authority["l2_receipt"]["sha256"]


@pytest.mark.parametrize("tamper", ["missing", "inventory"])
def test_historical_authority_rejects_missing_or_mismatched_capture(
        tmp_path, tamper):
    tree = _historical_metadata_tree(tmp_path)
    path = tree["quality"] / (
        "capture_gap_receipt_%s.json" % tree["date"])
    if tamper == "missing":
        path.unlink()
    else:
        value = json.loads(path.read_text())
        value["files"][0]["bytes"] += 1
        path.write_text(json.dumps(value, sort_keys=True) + "\n")

    with pytest.raises(cr.ReceiptError) as blocked:
        fcr.historical_authoritative_day_inputs(
            tree["date"], str(tree["raw_root"]), str(tree["warehouse"]),
            str(tree["quality"]))
    assert blocked.value.code == "HISTORICAL_METADATA_AUTHORITY_BLOCKED"
    assert "CAPTURE_RECEIPT_" in blocked.value.detail


@pytest.mark.parametrize("tamper", ["missing", "hash"])
def test_historical_authority_rejects_missing_or_changed_fact(
        tmp_path, tamper):
    tree = _historical_metadata_tree(tmp_path)
    fact = next(tree["facts"].rglob("*.parquet"))
    if tamper == "missing":
        fact.unlink()
    else:
        fact.write_bytes(b"X" * fact.stat().st_size)

    with pytest.raises(cr.ReceiptError) as blocked:
        fcr.historical_authoritative_day_inputs(
            tree["date"], str(tree["raw_root"]), str(tree["warehouse"]),
            str(tree["quality"]))
    assert blocked.value.code == "HISTORICAL_METADATA_AUTHORITY_BLOCKED"
    assert "SEALED_METADATA_INVALID" in blocked.value.detail


def test_historical_authority_rejects_boolean_l2_inventory_bytes(tmp_path):
    tree = _historical_metadata_tree(tmp_path)
    path = tree["quality"] / ("l2_gaps_%s.json" % tree["date"])
    value = json.loads(path.read_text())
    value["file_inventory"][0]["bytes"] = True
    path.write_text(json.dumps(value, sort_keys=True) + "\n")

    with pytest.raises(cr.ReceiptError) as blocked:
        fcr.historical_authoritative_day_inputs(
            tree["date"], str(tree["raw_root"]), str(tree["warehouse"]),
            str(tree["quality"]))
    assert blocked.value.code == "HISTORICAL_METADATA_AUTHORITY_BLOCKED"
    assert "L2_RECEIPT_INVALID" in blocked.value.detail


@pytest.mark.parametrize("tamper", ["extra", "table"])
def test_historical_authority_requires_exact_manifest_fact_projection(
        tmp_path, tamper):
    tree = _historical_metadata_tree(tmp_path)
    seal_path = (tree["warehouse"] / "seals"
                 / ("date=%s.json" % tree["date"]))
    seal = json.loads(seal_path.read_text())
    if tamper == "extra":
        payload = b"hidden-extra-fact"
        path = tree["facts"] / "hidden.bin"
        _write(path, payload)
        stat = path.stat()
        seal["archive_file_stats"].append({
            "file": "hidden.bin",
            "table": "trades",
            "size": len(payload),
            "sha256": _sha(payload),
            "md5": hashlib.md5(payload).hexdigest(),  # noqa: S324
            "inode": stat.st_ino,
            "mtime_ns": stat.st_mtime_ns,
            "ctime_ns": stat.st_ctime_ns,
        })
        seal["archive_files"] += 1
    else:
        seal["archive_file_stats"][0]["table"] = "trades"
    seal_path.write_text(json.dumps(seal, sort_keys=True, indent=2) + "\n")

    with pytest.raises(cr.ReceiptError) as blocked:
        fcr.historical_authoritative_day_inputs(
            tree["date"], str(tree["raw_root"]), str(tree["warehouse"]),
            str(tree["quality"]))
    assert blocked.value.code == "HISTORICAL_METADATA_AUTHORITY_BLOCKED"
    expected_reason = (
        "MANIFEST_FACT_PROJECTION_MISMATCH" if tamper == "extra"
        else "SEALED_METADATA_INVALID")
    assert expected_reason in blocked.value.detail


def test_historical_authority_rechecks_fact_fingerprint_at_return(
        tmp_path, monkeypatch):
    tree = _historical_metadata_tree(tmp_path)
    fact = next(tree["facts"].rglob("*.parquet"))
    original = fcr._historical_receipt

    def mutate_after_fact_hash(path, date, label):
        result = original(path, date, label)
        if label == "CAPTURE_RECEIPT":
            fact.write_bytes(b"Z" * fact.stat().st_size)
        return result

    monkeypatch.setattr(fcr, "_historical_receipt", mutate_after_fact_hash)
    with pytest.raises(cr.ReceiptError) as blocked:
        fcr.historical_authoritative_day_inputs(
            tree["date"], str(tree["raw_root"]), str(tree["warehouse"]),
            str(tree["quality"]))
    assert blocked.value.code == "HISTORICAL_METADATA_AUTHORITY_BLOCKED"
    assert "FACT_BYTES_CHANGED" in blocked.value.detail


def test_historical_authority_rejects_l2_facts_without_sealed_l2_source(
        tmp_path):
    tree = _historical_metadata_tree(tmp_path)
    seal_path = (tree["warehouse"] / "seals"
                 / ("date=%s.json" % tree["date"]))
    seal = json.loads(seal_path.read_text())
    seal["raw_files"] = [
        row for row in seal["raw_files"]
        if not Path(row["file"]).name.startswith("l2_")]
    seal_path.write_text(json.dumps(seal, sort_keys=True, indent=2) + "\n")

    with pytest.raises(cr.ReceiptError) as blocked:
        fcr.historical_authoritative_day_inputs(
            tree["date"], str(tree["raw_root"]), str(tree["warehouse"]),
            str(tree["quality"]))
    assert blocked.value.code == "HISTORICAL_METADATA_AUTHORITY_BLOCKED"
    assert "L2_SEAL_SOURCE_MISSING" in blocked.value.detail


def test_historical_authority_rejects_nonlegacy_and_forward_path_unchanged(
        tmp_path, monkeypatch):
    tree = _historical_metadata_tree(tmp_path)
    with pytest.raises(cr.ReceiptError, match="DATE_NOT_ALLOWLISTED"):
        fcr.historical_authoritative_day_inputs(
            "2026-07-17", str(tree["raw_root"]), str(tree["warehouse"]),
            str(tree["quality"]))

    sentinel = object()
    calls = []

    def ordinary(date, raw_root, warehouse_root):
        calls.append((date, raw_root, warehouse_root))
        return sentinel

    monkeypatch.setattr(cr, "_authoritative_day_inputs", ordinary)
    assert fcr._forward_authoritative_day_inputs(
        "2026-07-17", "raw", "warehouse", "quality") is sentinel
    assert calls == [("2026-07-17", "raw", "warehouse")]
