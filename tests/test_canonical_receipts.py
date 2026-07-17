#!/usr/bin/env python3
"""Offline contract tests for W-PUB-REF-01A canonical receipt shadowing.

These tests deliberately expose a very small S3 surface: ``head`` and
``get_exact``.  Shadow receipt construction is read/verify-only with respect
to canonical data; it must not tag, overwrite, or delete an S3 object, and it
must not alter the existing raw-prune path.
"""
import copy
import csv
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import canonical_receipts as cr  # noqa: E402
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
            DATE, table, "Sports", "Baseball", 1, str(path),
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
    payloads["%s/warehouse/manifest.csv" % PREFIX] = manifest_payload
    manifest_sha, _ = wc.manifest_date_sha256(str(manifest), DATE)

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
        payloads[
            "%s/warehouse/catalog/%s/part-00000.parquet" % (PREFIX, name)
        ] = catalog_payload

    correction_row = {
        "table": "trades",
        "row": [1, "fixture"],
        "observed_at_utc": "2026-07-14T03:00:00Z",
        "source_file": "/fixture/raw.ndjson",
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
            "source_file": "/fixture/raw.ndjson",
            "source_raw_rel": "date=%s/firehose_23.ndjson" % DATE,
            "seal_untouched": True,
        }, sort_keys=True) + "\n"
        + json.dumps({
            "event": "LATE_FACT_DIVERTED_TO_CORRECTIONS",
            "exchange_date": "2026-07-12",
            "n_rows": 1,
        }, sort_keys=True) + "\n",
    )
    payloads["%s/warehouse/corrections/ledger.ndjson" % PREFIX] = ledger_payload

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
            "gaps": [],
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

    return {
        "raw_root": raw_root,
        "warehouse": warehouse,
        "quality": quality,
        "seal_path": seal_path,
        "seal": seal,
        "payloads": payloads,
    }


class FakeExactVersionClient:
    """In-memory exact-version S3 double; intentionally no write methods."""

    def __init__(self, payloads, missing_version_for=None,
                 corrupt_exact_for=None, race_key=None, missing_key=None):
        self.ops = []
        self.latest = {}
        self.versions = {}
        self.missing_version_for = missing_version_for
        self.corrupt_exact_for = corrupt_exact_for
        self.race_key = race_key
        self.missing_key = missing_key
        for key, payload in payloads.items():
            object_id = (BUCKET, key)
            self.latest[object_id] = "v1"
            self.versions[(BUCKET, key, "v1")] = payload

    def head(self, bucket, key):
        self.ops.append(("head", bucket, key))
        if key == self.missing_key:
            raise cr.ReceiptError("PENDING_CANONICAL",
                                  "fixture object is not uploaded")
        object_id = (bucket, key)
        version_id = self.latest[object_id]
        payload = self.versions[(bucket, key, version_id)]
        result = {"ContentLength": len(payload)}
        if key != self.missing_version_for:
            result["VersionId"] = version_id
        if key == self.race_key:
            # Simulate a writer replacing latest immediately after HEAD.  A
            # correct verifier still GETs the returned v1, never mutable latest.
            self.versions[(bucket, key, "v2")] = b"raced-latest-bytes"
            self.latest[object_id] = "v2"
        return result

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


def test_inventory_maps_every_release_input_and_cross_day_raw(receipt_tree):
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
    assert set(by_key) == expected

    # Exact raw prefix/date mapping, including RFQ and the next receipt day.
    assert "%s/raw/date=%s/rfq_23.ndjson.2" % (PREFIX, DATE) in by_key
    cross_rfq = "%s/raw/date=%s/rfq_00.ndjson" % (PREFIX, NEXT_DATE)
    assert cross_rfq in by_key
    assert by_key[cross_rfq]["date"] == NEXT_DATE
    assert by_key[cross_rfq]["channel"] == "rfq"
    assert by_key[cross_rfq]["research_eligible"] is False
    assert by_key[cross_rfq]["research_candidate"] is False
    assert by_key[cross_rfq]["durability_scope"] is True
    assert by_key[cross_rfq]["exposure_policy"] == "FORBIDDEN_RFQ_DEFAULT"
    assert "rfq" in by_key[cross_rfq]["logical_source_key"]

    fact_key = "%s/warehouse/facts/orderbooks_l1/category=Sports/" \
               "subcategory=Baseball/date=%s/" \
               "orderbooks_l1__Sports__Baseball__%s.parquet" \
               % (PREFIX, DATE, DATE)
    assert by_key[fact_key]["table"] == "orderbooks_l1"
    assert by_key[fact_key]["source_kind"] == "facts"

    for key in (
        "%s/warehouse/seals/date=%s.json" % (PREFIX, DATE),
        "%s/warehouse/manifest.csv" % PREFIX,
        "%s/warehouse/dim/snapshots/date=%s/markets.csv" % (PREFIX, DATE),
        "%s/warehouse/catalog/markets/part-00000.parquet" % PREFIX,
        "%s/warehouse/corrections/date=%s/late_rows.ndjson" % (PREFIX, DATE),
        "%s/warehouse/corrections/ledger.ndjson" % PREFIX,
        "%s/control/quality/v1/date=%s/capture_gap_receipt.json" % (PREFIX, DATE),
        "%s/control/quality/v1/date=%s/l2_gaps.json" % (PREFIX, DATE),
    ):
        assert key in by_key

    # Shadow is an attestation phase, not an eligibility/tagging phase.
    assert all(obj["durability_verified"] is False for obj in objects)
    assert all(obj["research_eligible"] is False for obj in objects)
    assert all("SHADOW" in obj["eligibility_tag_state"] for obj in objects)


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


def test_unreadable_capture_receipt_is_incomplete_evidence(receipt_tree):
    _mark_capture_receipt_unreadable(receipt_tree)
    seal_binding, objects = _build(receipt_tree)
    families = {row["name"]: row for row in seal_binding["families"]}
    assert families["capture_gap_receipt"]["state"] == "INCOMPLETE"
    assert "unreadable" in families["capture_gap_receipt"]["reason_code"]
    assert not any(obj["family"] == "capture_gap_receipt"
                   for obj in objects)


def test_cli_unreadable_capture_stops_before_any_s3_call(receipt_tree,
                                                         monkeypatch):
    _mark_capture_receipt_unreadable(receipt_tree)
    output = receipt_tree["warehouse"].parent / "unreadable-pending"

    def forbid_s3(_executable):
        pytest.fail("local quality blocker must stop before creating S3 client")

    monkeypatch.setattr(cr, "AwsCliS3Client", forbid_s3)
    rc = cr.main([
        "shadow", "--date", DATE, "--bucket", BUCKET,
        "--prefix", PREFIX,
        "--raw-root", str(receipt_tree["raw_root"]),
        "--warehouse-root", str(receipt_tree["warehouse"]),
        "--quality-dir", str(receipt_tree["quality"]),
        "--output-root", str(output),
    ])
    assert rc == 3
    status = json.loads((output / ("date=%s" % DATE)
                         / "SHADOW_STATUS.json").read_text())
    assert status["state"] == "PENDING_QUALITY_GENERATION"
    assert status["complete"] is False
    assert not list(output.glob("date=*/receipt-*.json"))


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
    race_key = "%s/warehouse/manifest.csv" % PREFIX
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

    def client_factory(_executable):
        return FakeExactVersionClient(receipt_tree["payloads"])

    monkeypatch.setattr(cr, "AwsCliS3Client", client_factory)
    argv = [
        "shadow", "--date", DATE, "--bucket", BUCKET,
        "--prefix", PREFIX,
        "--raw-root", str(receipt_tree["raw_root"]),
        "--warehouse-root", str(receipt_tree["warehouse"]),
        "--quality-dir", str(receipt_tree["quality"]),
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
        "--output-root", str(output),
    ])
    assert rc == 3
    status = json.loads((output / ("date=%s" % DATE)
                         / "SHADOW_STATUS.json").read_text())
    assert status["state"] == "PENDING_QUALITY_CANONICAL"
    assert status["complete"] is False
    assert not list(output.glob("date=*/receipt-*.json"))
    assert {op[0] for op in client.ops} <= {"head", "get_exact"}


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
    client.get_exact(BUCKET, "ec2/example", "v1", str(tmp_path / "object"))

    assert calls[0][0:3] == ["aws-fixture", "s3api", "head-object"]
    assert calls[1][0:3] == ["aws-fixture", "s3api", "get-object"]
    assert "--version-id" in calls[1]
    assert calls[1][calls[1].index("--version-id") + 1] == "v1"
    assert {call[2] for call in calls} == {"head-object", "get-object"}


def test_shadow_verifier_has_no_s3_mutation_surface(receipt_tree):
    _seal_binding, objects = _build(receipt_tree)
    client, _verified, failures, complete = _verify_all(receipt_tree, objects)
    assert complete and not failures
    assert {op[0] for op in client.ops} <= {"head", "get_exact"}

    source = inspect.getsource(cr).lower().replace("_", "-")
    for forbidden in (
        "put-object", "copy-object", "create-multipart-upload",
        "put-object-tagging", "put-object-version-tagging", "delete-object",
        "delete-objects", "put-bucket-lifecycle", "put-object-legal-hold",
    ):
        assert forbidden not in source
