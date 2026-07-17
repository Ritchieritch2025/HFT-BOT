#!/usr/bin/env python3
"""Offline producer contract for immutable raw-prune authority."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import canonical_receipt_control as crc  # noqa: E402
import canonical_receipts as cr  # noqa: E402
import prune_raw  # noqa: E402


DATE = "2026-07-13"
NEXT_DATE = "2026-07-14"
BUCKET = "kalshi-vault-ritcardo"
PREFIX = "ec2"
MODIFIED = "2026-07-14T04:00:00Z"


def _sha(payload):
    return hashlib.sha256(payload).hexdigest()


def _verified_object(*, key, logical, kind, channel, family, payload,
                     seal_sha, date=DATE, required=True, exposure="FORBIDDEN_RAW"):
    obj = cr._entry(
        BUCKET, key, logical, kind, date, len(payload), _sha(payload),
        seal_sha, channel=channel, family=family,
        evidence_binding="seal.raw_files",
        attestation_class="DAY_SEAL_ATTESTED", required=required,
        durability_scope=True, research_candidate=False,
        exposure_policy=exposure,
        version_resolution="SEALED_CURRENT_EXACT")
    obj.update({
        "VersionId": "version-%s" % _sha(key.encode())[:16],
        "last_modified_utc": MODIFIED,
        "durability_verified": True,
        "verification_state": "EXACT_VERSION_FULL_SHA256",
    })
    return obj


def _receipt_fixture(tmp_path):
    seal_bytes = b'{"status":"SEALED"}\n'
    seal_sha = _sha(seal_bytes)
    seal = _verified_object(
        key="ec2/warehouse/seals/date=%s.json" % DATE,
        logical="warehouse/seals/date=%s.json" % DATE,
        kind="seal", channel="seal", family="seal", payload=seal_bytes,
        seal_sha=seal_sha, exposure="NOT_RESEARCH_EXPOSED")
    firehose = _verified_object(
        key="ec2/raw/date=%s/firehose_12.ndjson" % DATE,
        logical="raw/date=%s/firehose_12.ndjson" % DATE,
        kind="raw_firehose", channel="firehose", family="raw_durability",
        payload=b'{"channel":"ticker"}\n', seal_sha=seal_sha)
    l2 = _verified_object(
        key="ec2/raw/date=%s/l2_12.ndjson.1" % DATE,
        logical="raw/date=%s/l2_12.ndjson.1" % DATE,
        kind="raw_l2", channel="orderbooks_full", family="raw_durability",
        payload=b'{"channel":"orderbook_delta"}\n', seal_sha=seal_sha)
    rfq = _verified_object(
        key="ec2/raw/date=%s/rfq_12.ndjson.2" % DATE,
        logical="raw/date=%s/rfq_12.ndjson.2" % DATE,
        kind="raw_rfq", channel="rfq", family="raw_durability",
        payload=b'{"channel":"rfq"}\n', seal_sha=seal_sha,
        required=False, exposure="FORBIDDEN_RFQ_DEFAULT")
    cross = _verified_object(
        key="ec2/raw/date=%s/firehose_00.ndjson" % NEXT_DATE,
        logical="raw/date=%s/firehose_00.ndjson" % NEXT_DATE,
        kind="raw_firehose", channel="firehose", family="raw_durability",
        payload=b'{"channel":"ticker-next"}\n', seal_sha=seal_sha,
        date=NEXT_DATE)
    objects = [seal, firehose, l2, rfq, cross]
    for obj in objects:
        obj["_inventory_complete"] = True
        obj["_inventory_total"] = len(objects)
    families = cr._finalize_families([
        cr._family("seal", "REQUIRED_CORE", "seal", 1, 1,
                   "PRESENT_VERIFIED"),
        cr._family("raw_durability", "REQUIRED_CORE", "seal.raw_files", 4,
                   4, "PRESENT_VERIFIED"),
    ], objects)
    seal_binding = {
        "date": DATE,
        "status": "SEALED",
        "version": 2,
        "method": "full_v2",
        "bucket": BUCKET,
        "key": seal["key"],
        "size": seal["size"],
        "sha256": seal_sha,
        "manifest_date_sha256": "m" * 64,
        "families": families,
    }
    shadow_path = cr.write_shadow_receipt(
        tmp_path / "shadow", DATE, seal_binding, objects, MODIFIED,
        "a" * 40)
    receipt = json.loads(Path(shadow_path).read_text())
    receipt.update({
        "state": crc.DURABLE_STATE,
        "authority": crc.DURABLE_AUTHORITY,
        "authoritative": True,
        "s3_published": True,
    })
    body = crc._json_bytes(receipt)
    receipt_key = (
        "ec2/control/canonical-receipts/v1/date=%s/receipt-%s.json" %
        (DATE, receipt["receipt_set_sha256"]))
    binding = {
        "bucket": BUCKET,
        "key": receipt_key,
        "VersionId": "canonical-receipt-v1",
        "size": len(body),
        "sha256": _sha(body),
        "last_modified_utc": MODIFIED,
        "verification_state": "EXACT_VERSION_FULL_SHA256",
    }
    index = {
        "schema_version": crc.DURABLE_INDEX_SCHEMA,
        "state": crc.DURABLE_STATE,
        "date": DATE,
        "receipt_set_sha256": receipt["receipt_set_sha256"],
        "receipt_object": binding,
        "receipt_payload_size": len(body),
        "receipt_payload_sha256": _sha(body),
        "complete": True,
        "prune_eligible": False,
    }
    index_path = tmp_path / "DURABLE.json"
    index_path.write_text(json.dumps(index, sort_keys=True) + "\n")
    raw_payloads = {
        "firehose_12.ndjson": b'{"channel":"ticker"}\n',
        "l2_12.ndjson.1": b'{"channel":"orderbook_delta"}\n',
    }
    return {
        "index": index,
        "index_path": index_path,
        "receipt": receipt,
        "body": body,
        "binding": binding,
        "raw_payloads": raw_payloads,
    }


class ExactReader:
    def __init__(self, fixture, body=None):
        self.binding = fixture["binding"]
        self.body = fixture["body"] if body is None else body

    def head(self, bucket, key, version_id):
        assert (bucket, key, version_id) == (
            self.binding["bucket"], self.binding["key"],
            self.binding["VersionId"])
        return {
            "VersionId": version_id,
            "ContentLength": self.binding["size"],
            "LastModified": self.binding["last_modified_utc"],
        }

    def get_exact(self, bucket, key, version_id, path):
        assert (bucket, key, version_id) == (
            self.binding["bucket"], self.binding["key"],
            self.binding["VersionId"])
        Path(path).write_bytes(self.body)


class FixtureWriter:
    def __init__(self):
        self.stored = {}
        self.puts = 0
        self.reused = 0

    def ensure_bytes(self, bucket, key, payload):
        identity = (bucket, key)
        if identity in self.stored:
            if self.stored[identity] != payload:
                raise cr.ReceiptError("CONTROL_CONFLICT", key)
            self.reused += 1
        else:
            self.stored[identity] = payload
            self.puts += 1
        return {
            "bucket": bucket,
            "key": key,
            "VersionId": "prune-authority-v1",
            "size": len(payload),
            "sha256": _sha(payload),
            "last_modified_utc": MODIFIED,
            "verification_state": "EXACT_VERSION_FULL_SHA256",
        }


def _publish(fixture, writer, output):
    return crc.publish_prune_authority(
        DATE, BUCKET, PREFIX, fixture["index_path"], ExactReader(fixture),
        writer, output)


def test_success_is_consumer_compatible_and_excludes_rfq_and_cross_date(
        tmp_path):
    fixture = _receipt_fixture(tmp_path)
    writer = FixtureWriter()
    output = tmp_path / "authority"
    path, local, remote = _publish(fixture, writer, output)
    assert path == output / ("date=" + DATE) / prune_raw.PRUNE_AUTHORITY_NAME
    assert writer.puts == 1
    assert local["prune_eligible"] is True
    assert remote["eligibility_scope"] == \
        crc.PRUNE_ELIGIBILITY_SCOPE
    assert remote["rfq_state"] == "EXCLUDED_DEFERRED"
    assert remote["rfq_objects_deferred"] == 1
    assert remote["cross_date_objects_deferred"] == 1
    assert {row["local_raw_rel"] for row in local["objects"]} == {
        "date=%s/firehose_12.ndjson" % DATE,
        "date=%s/l2_12.ndjson.1" % DATE,
    }
    assert all("rfq" not in row["local_raw_rel"] for row in local["objects"])
    assert local["receipt_object"]["key"] == (
        "ec2/control/raw-prune-authority/v1/date=%s/receipt-%s.json" %
        (DATE, local["receipt_set_sha256"]))

    raw_dir = tmp_path / "raw" / ("date=" + DATE)
    raw_dir.mkdir(parents=True)
    for name, payload in fixture["raw_payloads"].items():
        (raw_dir / name).write_bytes(payload)
    loaded = prune_raw._load_prune_authority(output, DATE, raw_dir)
    assert set(loaded) == {row["local_raw_rel"] for row in local["objects"]}


def test_repeat_is_exact_noop(tmp_path):
    fixture = _receipt_fixture(tmp_path)
    writer = FixtureWriter()
    output = tmp_path / "authority"
    first = _publish(fixture, writer, output)
    before = first[0].read_bytes()
    second = _publish(fixture, writer, output)
    assert second[0].read_bytes() == before
    assert second[1] == first[1]
    assert writer.puts == 1
    assert writer.reused == 1


def test_remote_write_failure_leaves_no_local_authority(tmp_path):
    fixture = _receipt_fixture(tmp_path)

    class FailingWriter:
        @staticmethod
        def ensure_bytes(*_args):
            raise cr.ReceiptError("S3_CONTROL_PUT_FAILED", "fixture")

    output = tmp_path / "authority"
    with pytest.raises(cr.ReceiptError, match="S3_CONTROL_PUT_FAILED"):
        _publish(fixture, FailingWriter(), output)
    assert not (output / ("date=" + DATE) /
                prune_raw.PRUNE_AUTHORITY_NAME).exists()


@pytest.mark.parametrize("fault", ["null_version", "receipt_hash"])
def test_invalid_source_receipt_fails_before_write(tmp_path, fault):
    fixture = _receipt_fixture(tmp_path)
    if fault == "null_version":
        index = copy.deepcopy(fixture["index"])
        index["receipt_object"]["VersionId"] = "null"
        fixture["index_path"].write_text(json.dumps(index) + "\n")
        reader = ExactReader(fixture)
    else:
        reader = ExactReader(fixture, body=fixture["body"] + b"tamper")
    writer = FixtureWriter()
    output = tmp_path / "authority"
    with pytest.raises(cr.ReceiptError):
        crc.publish_prune_authority(
            DATE, BUCKET, PREFIX, fixture["index_path"], reader, writer,
            output)
    assert writer.puts == 0
    assert not output.exists()


def test_conflicting_local_authority_stops_before_remote_write(tmp_path):
    fixture = _receipt_fixture(tmp_path)
    output = tmp_path / "authority"
    path = output / ("date=" + DATE) / prune_raw.PRUNE_AUTHORITY_NAME
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"receipt_set_sha256": "0" * 64,
                                "objects": []}) + "\n")
    writer = FixtureWriter()
    with pytest.raises(cr.ReceiptError, match="PRUNE_AUTHORITY_CONFLICT"):
        _publish(fixture, writer, output)
    assert writer.puts == 0

