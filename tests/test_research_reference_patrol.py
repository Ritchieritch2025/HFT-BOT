#!/usr/bin/env python3
"""Offline tests for the read-only v3 reference-integrity patrol."""
import copy
import datetime
import inspect
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import research_reference_patrol as patrol  # noqa: E402
import research_release as rr  # noqa: E402
import test_research_reference_consumer as consumer  # noqa: E402


NOW = datetime.datetime(2026, 7, 17, 12, 0, tzinfo=datetime.timezone.utc)


class ExactMetadataReader:
    def __init__(self, manifest, source_bytes):
        self.ops = []
        self.source_bytes = dict(source_bytes)
        self.objects = {
            _identity(obj): copy.deepcopy(obj) for obj in manifest["objects"]
        }
        self.head_overrides = {}
        self.tag_overrides = {}

    def head(self, bucket, key, version_id):
        self.ops.append(("head", bucket, key, version_id))
        identity = (bucket, key, version_id)
        if identity not in self.objects:
            raise RuntimeError("fixture exact version is missing")
        obj = self.objects[identity]
        result = {
            "VersionId": version_id,
            "ContentLength": len(self.source_bytes[identity]),
            "LastModified": obj["source_last_modified_utc"],
        }
        result.update(self.head_overrides.get(identity, {}))
        return result

    def get_tags(self, bucket, key, version_id):
        self.ops.append(("get_tags", bucket, key, version_id))
        identity = (bucket, key, version_id)
        if identity not in self.objects:
            raise RuntimeError("fixture exact version is missing")
        obj = self.objects[identity]
        tags = self.tag_overrides.get(identity, {"research-eligible": "true"})
        if obj.get("kind") == "rfq" and identity not in self.tag_overrides:
            tags["research-channel"] = "rfq"
        return {
            "VersionId": version_id,
            "TagSet": [{"Key": key, "Value": value}
                       for key, value in sorted(tags.items())],
        }


@pytest.fixture
def patrol_fixture(tmp_path):
    _rid, manifest, source_bytes = consumer.build_release()
    manifest_path = tmp_path / "MANIFEST.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    return manifest_path, manifest, ExactMetadataReader(manifest, source_bytes)


def _policy_evidence(tmp_path, *, now=NOW, mutate=None):
    projection = {
        "schema_version": patrol.POLICY_EVIDENCE_SCHEMA,
        "status": "PASS",
        "generated_at_utc": (now - datetime.timedelta(minutes=5))
        .isoformat().replace("+00:00", "Z"),
        "expires_at_utc": (now + datetime.timedelta(hours=1))
        .isoformat().replace("+00:00", "Z"),
        "principal_arn": "arn:aws:iam::123456789012:role/fixture-reader",
        "covered_buckets": ["kalshi-vault-ritcardo"],
        "policy_snapshot": {
            "identity_policy_sha256": "1" * 64,
            "bucket_policy_sha256": "2" * 64,
            "effective_permissions_sha256": "3" * 64,
        },
    }
    evidence = copy.deepcopy(projection)
    evidence["evidence_sha256"] = patrol.canonical_sha256(projection)
    if mutate is not None:
        mutate(evidence)
    path = tmp_path / "policy-evidence.json"
    path.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n")
    return path


def _paths(tmp_path):
    return tmp_path / "YELLOW.json", tmp_path / "PATROL_STATUS.json"


def _identity(obj):
    return obj["source_bucket"], obj["source_key"], obj["source_version_id"]


def test_patrol_passes_all_exact_metadata_and_tags(patrol_fixture, tmp_path):
    manifest_path, manifest, reader = patrol_fixture
    policy = _policy_evidence(tmp_path)
    alert, status = _paths(tmp_path)
    result = patrol.run_patrol(
        [manifest_path], reader, str(policy), str(alert), str(status), NOW)

    assert result["state"] == "PASS"
    assert result["freeze_new_v3_publication"] is False
    assert result["policy_evidence"]["covered"] is True
    assert result["object_checks_expected"] == len(manifest["objects"])
    assert result["object_checks_passed"] == len(manifest["objects"])
    assert not alert.exists()
    assert json.loads(status.read_text()) == result
    assert [op[0] for op in reader.ops].count("head") == len(manifest["objects"])
    assert [op[0] for op in reader.ops].count("get_tags") == \
        len(manifest["objects"])


@pytest.mark.parametrize("drift,expected_code", [
    ("missing", "EXACT_HEAD_FAILED"),
    ("tag", "ELIGIBILITY_TAG_DRIFT"),
    ("storage", "STORAGE_NOT_IMMEDIATELY_READABLE"),
    ("delete", "EXACT_VERSION_DELETE_MARKER"),
    ("size", "SIZE_DRIFT"),
    ("modified", "LAST_MODIFIED_DRIFT"),
])
def test_missing_tag_or_storage_drift_writes_durable_yellow(
        patrol_fixture, tmp_path, drift, expected_code):
    manifest_path, manifest, reader = patrol_fixture
    obj = manifest["objects"][0]
    identity = _identity(obj)
    if drift == "missing":
        reader.objects.pop(identity)
    elif drift == "tag":
        reader.tag_overrides[identity] = {}
    elif drift == "storage":
        reader.head_overrides[identity] = {"StorageClass": "DEEP_ARCHIVE"}
    elif drift == "delete":
        reader.head_overrides[identity] = {"DeleteMarker": True}
    elif drift == "size":
        reader.head_overrides[identity] = {
            "ContentLength": manifest["objects"][0]["size"] + 1}
    else:
        reader.head_overrides[identity] = {
            "LastModified": "2026-07-13T04:00:01Z"}
    policy = _policy_evidence(tmp_path)
    alert, status = _paths(tmp_path)

    result = patrol.run_patrol(
        [manifest_path], reader, str(policy), str(alert), str(status), NOW)
    yellow = json.loads(alert.read_text())
    assert result["state"] == "YELLOW"
    assert result["freeze_new_v3_publication"] is True
    assert yellow["alert_state"] == "ACTIVE_YELLOW"
    assert yellow["freeze_new_v3_publication"] is True
    assert expected_code in {row["code"] for row in yellow["failures"]}
    assert json.loads(status.read_text())["state"] == "YELLOW"


def test_success_never_automatically_clears_existing_yellow(
        patrol_fixture, tmp_path):
    manifest_path, manifest, reader = patrol_fixture
    identity = _identity(manifest["objects"][0])
    reader.tag_overrides[identity] = {}
    policy = _policy_evidence(tmp_path)
    alert, status = _paths(tmp_path)
    failed = patrol.run_patrol(
        [manifest_path], reader, str(policy), str(alert), str(status), NOW)
    assert failed["state"] == "YELLOW"
    yellow_before = alert.read_bytes()

    reader.tag_overrides.pop(identity)
    reader.ops.clear()
    passed = patrol.run_patrol(
        [manifest_path], reader, str(policy), str(alert), str(status),
        NOW + datetime.timedelta(minutes=1))
    assert passed["state"] == "PASS"
    assert passed["existing_yellow_hold"] is True
    assert passed["freeze_new_v3_publication"] is True
    assert alert.read_bytes() == yellow_before
    assert json.loads(status.read_text())["state"] == "PASS"


def test_policy_evidence_is_required_fresh_and_digest_bound(
        patrol_fixture, tmp_path):
    manifest_path, _manifest, reader = patrol_fixture
    missing_policy = tmp_path / "missing-policy.json"
    alert, status = _paths(tmp_path)
    result = patrol.run_patrol(
        [manifest_path], reader, str(missing_policy), str(alert), str(status),
        NOW)
    assert result["state"] == "YELLOW"
    assert result["policy_evidence"] == {"covered": False}
    assert {row["code"] for row in result["failures"]} == {
        "LOCAL_READ_FAILED"}
    assert reader.ops == []

    alert.unlink()
    tampered = _policy_evidence(
        tmp_path, mutate=lambda evidence: evidence["policy_snapshot"].update(
            {"bucket_policy_sha256": "9" * 64}))
    result = patrol.run_patrol(
        [manifest_path], reader, str(tampered), str(alert), str(status), NOW)
    assert {row["code"] for row in result["failures"]} == {
        "POLICY_EVIDENCE_DIGEST_MISMATCH"}
    assert reader.ops == []

    alert.unlink()
    stale = _policy_evidence(tmp_path, now=NOW - datetime.timedelta(days=2))
    result = patrol.run_patrol(
        [manifest_path], reader, str(stale), str(alert), str(status), NOW)
    assert {row["code"] for row in result["failures"]} == {
        "POLICY_EVIDENCE_STALE"}
    assert reader.ops == []


def test_manifest_validation_is_strict_and_stops_before_reader(
        patrol_fixture, tmp_path):
    manifest_path, manifest, reader = patrol_fixture
    manifest["publication_status"] = "DRAFT"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n")
    policy = _policy_evidence(tmp_path)
    alert, status = _paths(tmp_path)
    result = patrol.run_patrol(
        [manifest_path], reader, str(policy), str(alert), str(status), NOW)
    assert result["state"] == "YELLOW"
    assert {row["code"] for row in result["failures"]} == {"MANIFEST_INVALID"}
    assert reader.ops == []


def test_aws_adapter_has_only_exact_read_operations():
    source = inspect.getsource(patrol.AwsCliReadOnlyReader)
    assert "head-object" in source
    assert "get-object-tagging" in source
    for forbidden in (
        "put-object", "delete-object", "copy-object", "put-object-tagging",
        "delete-object-tagging", "create-multipart-upload",
    ):
        assert forbidden not in source


def test_patrol_and_publisher_share_alert_and_readability_contract(tmp_path):
    live_dir = tmp_path / "main-checkout" / "work" / "live"
    assert patrol.alert_path_for_live_dir(str(live_dir)) == \
        rr.reference_yellow_alert_path(str(live_dir))
    assert patrol.READABLE_STORAGE_CLASSES == \
        rr.REFERENCE_READABLE_STORAGE_CLASSES
