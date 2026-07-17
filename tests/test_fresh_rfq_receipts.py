"""Offline contracts for the isolated post-T0 RFQ receipt lane."""

import copy
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "fresh_rfq_receipts.py"


def load_module():
    spec = importlib.util.spec_from_file_location("fresh_rfq_receipts", TOOL)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


fresh = load_module()


def sha(label):
    return hashlib.sha256(label.encode()).hexdigest()


def deny_rows(overlap=None):
    rows = [] if overlap is None else [overlap]
    for index in range(284 - len(rows)):
        rows.append({
            "key": f"raw_rfq/date=2026-06-{1 + index // 24:02d}/"
                   f"rfq_{index % 24:02d}.ndjson.{1000 + index}",
            "size": index + 1,
            "sha256": sha(f"old-{index}"),
        })
    return rows


def authority(monkeypatch, *, t0="2026-07-17T00:00:00Z", overlap=None):
    rows = deny_rows(overlap)
    vector_sha = fresh.old_object_set_sha256(rows)
    monkeypatch.setattr(fresh, "OLD_284_OBJECT_SET_SHA256", vector_sha)
    return fresh.build_fresh_epoch_authority(
        operator_authorization_sha256=fresh.OPERATOR_AUTHORIZATION_SHA256,
        strict_t0_utc=t0,
        created_at_utc="2026-07-16T23:59:00Z",
        deployment_commit="a" * 40,
        generation="rfq-capture-generation-7",
        deny_identities=rows,
    )


def segment(auth, hour="2026-07-17T00", *, shard_count=1,
            supervisor_pid=4100, child_pid=4200, child_generation=3,
            subscription_acks=1, subscription_end=True):
    start = dt.datetime.strptime(hour, "%Y-%m-%dT%H").replace(
        tzinfo=dt.timezone.utc)
    start_ns = int(start.timestamp()) * 1_000_000_000
    date, hh = hour.split("T")
    base = f"date={date}/rfq_{hh}.ndjson"
    shards = []
    for ordinal in range(shard_count):
        shards.append({
            "ordinal": ordinal,
            "relpath": base if ordinal == 0 else f"{base}.{ordinal}",
            "bytes_before": 0,
            "size": 100 + ordinal,
            "parsed_bytes_at_close": 100 + ordinal,
            "sha256": sha(f"{hour}-shard-{ordinal}"),
        })
    return {
        "type": "rfq_segment_receipt",
        "schema": "rfq-segment-receipt-v3",
        "lane_id": auth["lane_id"],
        "authority_sha256": auth["authority_sha256"],
        "deployment_commit": auth["deployment_commit"],
        "generation": auth["generation"],
        "fresh_lane_state": "BOUND_AUTHORITY",
        "supervisor_pid": supervisor_pid,
        "child_pid": child_pid,
        "child_generation": child_generation,
        "segment_hour": hour,
        "expected_start_wall_ns": start_ns,
        "expected_end_wall_ns": start_ns + 3_600_000_000_000,
        "child_started_wall_ns": start_ns - 1_000_000,
        "segment_started_wall_ns": start_ns,
        "receipt_observed_wall_ns": start_ns + 3_601_000_000_000,
        "start_lag_ms": 0,
        "end_early_ms": 0,
        "end_reason": "boundary",
        "boundary_closed": True,
        "close_stability_ms": 1000,
        "child_rc": None,
        "status": "PASS",
        "subscription_proven": True,
        "capture_relpath": base,
        "capture_origin_path": f"/srv/rfq/{base}",
        "capture_bytes_before": 0,
        "capture_bytes_at_close": sum(row["size"] for row in shards),
        "capture_sha256_at_close": shards[0]["sha256"]
        if shard_count == 1 else None,
        "capture_shards": shards,
        "capture_shard_count": shard_count,
        "capture_shard_set_sha256": fresh.canonical_sha256(shards),
        "raw_evidence": {
            "findings": [], "recorder_rows": 3,
            "subscribed_communications": subscription_acks,
            "subscription_ack_wall_ns":
                start_ns + 1_000_000_000 if subscription_acks else None,
            "rfq_created": 0, "rfq_deleted": 0,
            "markers": {"hour_open": 1}, "partition_mismatches": 0,
            "max_stream_epoch": 1,
            "subscription_invalidations": 0,
            "subscription_proven_at_end": subscription_end,
            "start_offsets": {},
            "end_offsets": {f"/srv/rfq/{base}": sum(
                row["size"] for row in shards)},
            "shards": [f"/srv/rfq/{row['relpath']}" for row in shards],
        },
        "metrics_evidence": {
            "findings": [], "feed_rows": 60, "connected_valid_rows": 60,
            "min_reconnects": 0, "min_disconnects": 0, "min_errors": 0,
            "first_ts_ms": start_ns // 1_000_000,
            "last_ts_ms": (start_ns + 3_600_000_000_000) // 1_000_000 - 1,
            "max_reconnects": 0, "max_disconnects": 0, "max_errors": 0,
            "max_recorder_dropped": 0, "max_recorder_write_failures": 0,
            "end_offset": 1000, "next_window_offset": 1000,
        },
        "findings": [],
        "generated_at_utc": "2026-07-17T01:00:02Z",
    }


def inventory(receipt, prefix="ec2/raw"):
    return [{
        "key": f"{prefix}/{row['relpath']}",
        "version_id": f"version-{row['ordinal']}",
        "size": row["size"],
        "sha256": row["sha256"],
    } for row in receipt["capture_shards"]]


def test_historical_fingerprint_algorithm_and_fixed_semantics():
    rows = [
        {"key": "raw_rfq/z", "size": 2, "sha256": "b" * 64},
        {"key": "raw_rfq/a", "size": 1, "sha256": "a" * 64},
    ]
    expected = hashlib.sha256(
        (f"raw_rfq/a\t1\t{'a' * 64}\n"
         f"raw_rfq/z\t2\t{'b' * 64}").encode()).hexdigest()
    assert fresh.old_object_set_sha256(rows) == expected
    assert fresh.OLD_284_OBJECT_SET_SHA256 == (
        "1873803765e70de69f4b398dcea7e4dc66c2749950c9d5f8c0d2d198c5087c71")
    assert fresh.BLOCKED_SELECTION_SHA256 == (
        "8b310c37f3989770d1f53a058e5ef396e1eb5c9c3d24d07ac87bd9d8aee5b9e1")
    assert fresh.OLD_284_OBJECT_SET_SHA256 != fresh.BLOCKED_SELECTION_SHA256


def test_authority_binds_complete_deny_set_t0_and_capture_generation(monkeypatch):
    auth = authority(monkeypatch)
    assert auth["old_284_object_count"] == 284
    assert auth["old_lineage_state"] == "DATA_INTEGRITY_BLOCKED"
    assert auth["repair_state"] == "FORBIDDEN"
    assert auth["blocked_logical_prefix"] == "raw_rfq"
    assert auth["source_bucket"] == "kalshi-vault-ritcardo"
    assert auth["raw_key_prefix"] == "ec2/raw"
    assert auth["capture_template"] == \
        "date={UTC_DATE}/rfq_{UTC_HOUR}.ndjson"
    assert auth["credential_scope"]["kalshi_api_key_scope"] == "read"
    assert auth["authority_sha256"] == fresh.canonical_sha256({
        key: value for key, value in auth.items() if key != "authority_sha256"
    })

    broken = copy.deepcopy(auth)
    broken["old_284_object_set_sha256"] = fresh.BLOCKED_SELECTION_SHA256
    unsigned = {key: value for key, value in broken.items()
                if key != "authority_sha256"}
    broken["authority_sha256"] = fresh.canonical_sha256(unsigned)
    with pytest.raises(fresh.FreshRfqError, match="OLD_LINEAGE_BINDING"):
        fresh.validate_fresh_epoch_authority(broken)

    wrong_operator = copy.deepcopy(auth)
    wrong_operator["operator_authorization_sha256"] = sha("other approval")
    wrong_operator["authority_sha256"] = fresh.canonical_sha256({
        key: value for key, value in wrong_operator.items()
        if key != "authority_sha256"
    })
    with pytest.raises(fresh.FreshRfqError) as error:
        fresh.validate_fresh_epoch_authority(wrong_operator)
    assert error.value.code == "OPERATOR_AUTHORIZATION_BINDING"


@pytest.mark.parametrize("generation", [
    "", "contains space", "contains/slash", "é", "x" * 65,
])
def test_authority_rejects_unsafe_or_oversized_generation(
        monkeypatch, generation):
    auth = authority(monkeypatch)
    auth["generation"] = generation
    auth["authority_sha256"] = fresh.canonical_sha256({
        key: value for key, value in auth.items() if key != "authority_sha256"
    })
    with pytest.raises(fresh.FreshRfqError) as error:
        fresh.validate_fresh_epoch_authority(auth)
    assert error.value.code == "GENERATION_BINDING"


@pytest.mark.parametrize("mutation,code", [
    ({"schema": "rfq-segment-receipt-v2"}, "V2_DIAGNOSTIC_ONLY"),
    ({"status": "PARTIAL_START"}, "SEGMENT_NOT_PASS"),
    ({"status": "FAIL"}, "SEGMENT_NOT_PASS"),
    ({"findings": ["gap"]}, "SEGMENT_NOT_PASS"),
    ({"boundary_closed": False}, "SEGMENT_NOT_PASS"),
    ({"close_stability_ms": 999}, "SEGMENT_BOUNDARY"),
    ({"authority_sha256": "0" * 64}, "SEGMENT_AUTHORITY_BINDING"),
    ({"generation": "another"}, "SEGMENT_AUTHORITY_BINDING"),
])
def test_segment_rejects_non_strict_or_unbound_receipts(
        monkeypatch, mutation, code):
    auth = authority(monkeypatch)
    receipt = segment(auth)
    receipt.update(mutation)
    with pytest.raises(fresh.FreshRfqError) as error:
        fresh.validate_v3_segment_receipt(receipt, auth)
    assert error.value.code == code


def test_segment_accepts_complete_contiguous_multishard_set(monkeypatch):
    auth = authority(monkeypatch)
    receipt = segment(auth, shard_count=3)
    got = fresh.validate_v3_segment_receipt(receipt, auth)
    assert [row["ordinal"] for row in got["capture_shards"]] == [0, 1, 2]
    assert got["capture_shard_set_sha256"] == fresh.canonical_sha256(
        got["capture_shards"])


@pytest.mark.parametrize("field,value", [
    ("fresh_lane_state", "UNBOUND"),
    ("fresh_lane_state", "BOUND_PRE_T0_DIAGNOSTIC"),
    ("fresh_lane_state", "BOUND_AUTHORITY_INVALID"),
    ("supervisor_pid", 0),
    ("supervisor_pid", True),
    ("child_pid", -1),
    ("child_pid", "4200"),
    ("child_generation", 0),
    ("child_generation", None),
])
def test_segment_requires_bound_authority_and_positive_process_generation(
        monkeypatch, field, value):
    auth = authority(monkeypatch)
    receipt = segment(auth)
    receipt[field] = value
    with pytest.raises(fresh.FreshRfqError) as error:
        fresh.validate_v3_segment_receipt(receipt, auth)
    assert error.value.code == "SEGMENT_AUTHORITY_BINDING"


def test_persistent_generation_carries_subscription_proof_across_hours(monkeypatch):
    auth = authority(monkeypatch)
    receipt = segment(
        auth, "2026-07-17T01", subscription_acks=0,
        subscription_end=None)
    assert receipt["subscription_proven"] is True
    got = fresh.validate_v3_segment_receipt(receipt, auth)
    assert got["raw_evidence"]["subscription_proven_at_end"] is None

    receipt["raw_evidence"]["subscription_proven_at_end"] = False
    with pytest.raises(fresh.FreshRfqError) as error:
        fresh.validate_v3_segment_receipt(receipt, auth)
    assert error.value.code == "RAW_COVERAGE"


@pytest.mark.parametrize("mutation,code", [
    ("parsed", "TORN_SHARD"),
    ("raw_missing", "RAW_COVERAGE"),
    ("raw_count", "RAW_COVERAGE"),
    ("raw_forbidden_marker", "RAW_COVERAGE"),
    ("raw_extra", "RAW_COVERAGE"),
    ("metrics_missing", "METRICS_COVERAGE"),
    ("metrics_extra", "METRICS_COVERAGE"),
    ("metrics_coverage", "METRICS_COVERAGE"),
    ("metrics_reconnect", "METRICS_COVERAGE"),
    ("start_time", "SEGMENT_BOUNDARY"),
])
def test_segment_rejects_torn_or_incomplete_evidence(monkeypatch, mutation, code):
    auth = authority(monkeypatch)
    receipt = segment(auth)
    if mutation == "parsed":
        receipt["capture_shards"][0]["parsed_bytes_at_close"] -= 1
    elif mutation == "raw_missing":
        receipt["raw_evidence"].pop("partition_mismatches")
    elif mutation == "raw_count":
        receipt["raw_evidence"]["markers"]["hour_open"] = 2
    elif mutation == "raw_forbidden_marker":
        receipt["raw_evidence"]["markers"]["transport_close"] = 1
    elif mutation == "raw_extra":
        receipt["raw_evidence"]["trusted_because_findings_empty"] = True
    elif mutation == "metrics_missing":
        receipt["metrics_evidence"].pop("last_ts_ms")
    elif mutation == "metrics_extra":
        receipt["metrics_evidence"]["trusted_because_findings_empty"] = True
    elif mutation == "metrics_coverage":
        receipt["metrics_evidence"]["connected_valid_rows"] = 47
    elif mutation == "metrics_reconnect":
        receipt["metrics_evidence"]["max_reconnects"] = 1
    else:
        receipt["segment_started_wall_ns"] += 6_000_000_000
    with pytest.raises(fresh.FreshRfqError) as error:
        fresh.validate_v3_segment_receipt(receipt, auth)
    assert error.value.code == code


@pytest.mark.parametrize("change,code", [
    ("gap", "SHARD_GAP"),
    ("unsafe", "UNSAFE_PATH"),
    ("set_sha", "SHARD_SET_DIGEST"),
    ("size", "SHARD_SET_DIGEST"),
])
def test_segment_rejects_bad_shard_contract(monkeypatch, change, code):
    auth = authority(monkeypatch)
    receipt = segment(auth, shard_count=2)
    if change == "gap":
        receipt["capture_shards"][1]["ordinal"] = 2
    elif change == "unsafe":
        receipt["capture_shards"][1]["relpath"] = "date=2026-07-17/../secret"
    elif change == "set_sha":
        receipt["capture_shard_set_sha256"] = "0" * 64
    else:
        receipt["capture_bytes_at_close"] += 1
    with pytest.raises(fresh.FreshRfqError) as error:
        fresh.validate_v3_segment_receipt(receipt, auth)
    assert error.value.code == code


def test_pre_t0_fails_before_resolver_callback(monkeypatch):
    auth = authority(monkeypatch, t0="2026-07-17T01:00:00Z")
    receipt = segment(auth, "2026-07-17T00")
    calls = []

    def resolver(row):
        calls.append(row)
        return row

    with pytest.raises(fresh.FreshRfqError) as error:
        fresh.build_hour_receipt(
            authority=auth, segment_receipt=receipt,
            exact_inventory=inventory(receipt), raw_key_prefix="ec2/raw",
            resolver=resolver,
        )
    assert error.value.code == "BEFORE_STRICT_T0"
    assert calls == []


def test_hour_receipt_binds_exact_versions_and_is_order_stable(monkeypatch):
    auth = authority(monkeypatch)
    receipt = segment(auth, shard_count=3)
    rows = inventory(receipt)
    calls = []

    def resolver(row):
        calls.append(row["key"])
        return row

    one = fresh.build_hour_receipt(
        authority=auth, segment_receipt=receipt, exact_inventory=rows,
        raw_key_prefix="ec2/raw", resolver=resolver)
    two = fresh.build_hour_receipt(
        authority=auth, segment_receipt=receipt,
        exact_inventory=list(reversed(rows)), raw_key_prefix="ec2/raw")
    assert one["resolution_state"] == "RESOLVED_EXACT"
    assert two["resolution_state"] == "NOT_REQUESTED"
    assert one["rfq_object_set_sha256"] == two["rfq_object_set_sha256"]
    assert [row["key"] for row in one["rfq_objects"]] == [
        f"ec2/raw/{row['relpath']}" for row in receipt["capture_shards"]]
    assert len(calls) == 3


def test_optional_source_seal_is_exactly_bound(monkeypatch):
    auth = authority(monkeypatch)
    receipt = segment(auth)
    seal = {
        "bucket": fresh.SOURCE_BUCKET,
        "key": "ec2/warehouse/seals/date=2026-07-17.json",
        "version_id": "seal-version",
        "size": 123,
        "sha256": sha("seal bytes"),
        "verification_state": "PASS",
    }
    built = fresh.build_hour_receipt(
        authority=auth, segment_receipt=receipt,
        exact_inventory=inventory(receipt), raw_key_prefix="ec2/raw",
        source_seal=seal)
    assert built["source_seal"] == seal
    assert built["source_seal_binding_sha256"] == seal["sha256"]

    wrong = dict(seal, bucket="another-bucket")
    with pytest.raises(fresh.FreshRfqError, match="SOURCE_SEAL_INVALID"):
        fresh.build_hour_receipt(
            authority=auth, segment_receipt=receipt,
            exact_inventory=inventory(receipt), raw_key_prefix="ec2/raw",
            source_seal=wrong)


def test_hour_builder_rejects_prefix_outside_authority(monkeypatch):
    auth = authority(monkeypatch)
    receipt = segment(auth)
    with pytest.raises(fresh.FreshRfqError) as error:
        fresh.build_hour_receipt(
            authority=auth, segment_receipt=receipt,
            exact_inventory=inventory(receipt, "other/raw"),
            raw_key_prefix="other/raw")
    assert error.value.code == "SOURCE_SCOPE"


@pytest.mark.parametrize("mutation,code", [
    ("extra", "INVENTORY_NOT_EXACT"),
    ("missing", "INVENTORY_NOT_EXACT"),
    ("size", "INVENTORY_MISMATCH"),
    ("sha", "INVENTORY_MISMATCH"),
    ("duplicate", "INVENTORY_DUPLICATE"),
])
def test_inventory_is_bidirectionally_exact(monkeypatch, mutation, code):
    auth = authority(monkeypatch)
    receipt = segment(auth, shard_count=2)
    rows = inventory(receipt)
    if mutation == "extra":
        extra = copy.deepcopy(rows[-1])
        extra["key"] += ".2"
        rows.append(extra)
    elif mutation == "missing":
        rows.pop()
    elif mutation == "size":
        rows[0]["size"] += 1
    elif mutation == "sha":
        rows[0]["sha256"] = "0" * 64
    else:
        rows.append(copy.deepcopy(rows[0]))
    with pytest.raises(fresh.FreshRfqError) as error:
        fresh.build_hour_receipt(
            authority=auth, segment_receipt=receipt,
            exact_inventory=rows, raw_key_prefix="ec2/raw")
    assert error.value.code == code


def test_content_identity_catches_old_lineage_under_a_different_key(monkeypatch):
    hour = "2026-07-17T00"
    auth_seed = {"lane_id": fresh.LANE_ID, "authority_sha256": "0" * 64,
                 "deployment_commit": "a" * 40,
                 "generation": "rfq-capture-generation-7"}
    provisional = segment(auth_seed, hour)
    shard = provisional["capture_shards"][0]
    overlap = {
        "key": "raw_rfq/historical-copy/renamed-object.ndjson",
        "size": shard["size"],
        "sha256": shard["sha256"],
    }
    auth = authority(monkeypatch, overlap=overlap)
    receipt = segment(auth, hour)
    with pytest.raises(fresh.FreshRfqError) as error:
        fresh.build_hour_receipt(
            authority=auth, segment_receipt=receipt,
            exact_inventory=inventory(receipt), raw_key_prefix="ec2/raw")
    assert error.value.code == "OLD_LINEAGE_OVERLAP"


def test_resolver_mismatch_and_hour_digest_tamper_fail_closed(monkeypatch):
    auth = authority(monkeypatch)
    receipt = segment(auth)

    def wrong(row):
        row["version_id"] = "different"
        return row

    with pytest.raises(fresh.FreshRfqError, match="RESOLVER_MISMATCH"):
        fresh.build_hour_receipt(
            authority=auth, segment_receipt=receipt,
            exact_inventory=inventory(receipt), raw_key_prefix="ec2/raw",
            resolver=wrong)
    built = fresh.build_hour_receipt(
        authority=auth, segment_receipt=receipt,
        exact_inventory=inventory(receipt), raw_key_prefix="ec2/raw")
    tampered = json.loads(json.dumps(built))
    tampered["rfq_objects"][0]["version_id"] = "changed-after-receipt"
    with pytest.raises(fresh.FreshRfqError):
        fresh.validate_hour_receipt(tampered, auth)


def overlay_inputs(monkeypatch):
    auth = authority(monkeypatch)
    start = dt.datetime(2026, 7, 17, tzinfo=dt.timezone.utc)
    receipts = []
    for offset in range(26):
        hour = (start + dt.timedelta(hours=offset)).strftime("%Y-%m-%dT%H")
        seg = segment(
            auth, hour, subscription_acks=1 if offset == 0 else 0,
            subscription_end=True if offset == 0 else None)
        receipts.append(fresh.build_hour_receipt(
            authority=auth, segment_receipt=seg,
            exact_inventory=inventory(seg), raw_key_prefix="ec2/raw",
            resolver=lambda row: row))
    sealed = []
    for receipt in receipts[:24]:
        sealed.extend({
            "key": row["key"], "version_id": row["version_id"],
            "size": row["size"], "sha256": row["sha256"],
        } for row in receipt["rfq_objects"])
    sealed.sort(key=lambda row: row["key"])
    release_id = "2026-07-17__v3ref__seal-deadbeef__pub-0123456789abcdef"
    base = {
        "release_id": release_id,
        "date": "2026-07-17",
        "manifest_bucket": "research-bucket",
        "manifest_key": f"research/releases/{release_id}/MANIFEST.json",
        "manifest_version_id": "manifest-version",
        "manifest_sha256": sha("manifest"),
        "reference_set_sha256": sha("reference set"),
        "canonical_receipt_set_sha256": sha("canonical receipt set"),
        "verification_state": "REFERENCE_V3_VERIFIED",
        "source_seal": {
            "bucket": fresh.SOURCE_BUCKET,
            "key": "ec2/warehouse/seals/date=2026-07-17.json",
            "version_id": "seal-version",
            "size": 1234,
            "sha256": sha("D seal"),
            "verification_state": "PASS",
        },
        "sealed_rfq_objects": sealed,
        "sealed_rfq_object_set_sha256": fresh.canonical_sha256(sealed),
    }
    return auth, receipts, base


def test_overlay_has_exact_24_analysis_plus_2_watermark_without_copy(monkeypatch):
    auth, receipts, base = overlay_inputs(monkeypatch)
    one = fresh.build_overlay_manifest(
        authority=auth, base_release=base, hour_receipts=receipts)
    two = fresh.build_overlay_manifest(
        authority=auth, base_release=base,
        hour_receipts=list(reversed(receipts)))
    assert one == two
    assert len(one["analysis_hours"]) == 24
    assert len(one["watermark_hours"]) == 2
    assert one["analysis_hours"][0]["segment_hour"] == "2026-07-17T00"
    assert one["analysis_hours"][-1]["segment_hour"] == "2026-07-17T23"
    assert [row["segment_hour"] for row in one["watermark_hours"]] == [
        "2026-07-18T00", "2026-07-18T01"]
    assert one["analysis_rfq_objects"] == base["sealed_rfq_objects"]
    assert {row["key"] for row in one["analysis_rfq_objects"]}.isdisjoint(
        {row["key"] for row in one["watermark_rfq_objects"]})
    assert one["watermark_objects_in_analysis"] is False
    assert one["data_objects_copied"] == 0
    assert one["aws_write_authorized"] is False
    assert one["research_eligible"] is False


@pytest.mark.parametrize("mutation,code", [
    ("gap", "HOUR_CONTINUITY"),
    ("duplicate", "HOUR_DUPLICATE"),
    ("sealed_subset", "ANALYSIS_SEAL_MISMATCH"),
    ("base_manifest_sha", "INVALID_SHA256"),
    ("base_version", "VERSION_REQUIRED"),
])
def test_overlay_rejects_gap_duplicate_or_bad_base_binding(
        monkeypatch, mutation, code):
    auth, receipts, base = overlay_inputs(monkeypatch)
    if mutation == "gap":
        receipts.pop(10)
    elif mutation == "duplicate":
        receipts[-1] = receipts[-2]
    elif mutation == "sealed_subset":
        base["sealed_rfq_objects"][-1] = {
            "key": receipts[24]["rfq_objects"][0]["key"],
            "version_id": receipts[24]["rfq_objects"][0]["version_id"],
            "size": receipts[24]["rfq_objects"][0]["size"],
            "sha256": receipts[24]["rfq_objects"][0]["sha256"],
        }
        base["sealed_rfq_objects"].sort(key=lambda row: row["key"])
        base["sealed_rfq_object_set_sha256"] = fresh.canonical_sha256(
            base["sealed_rfq_objects"])
    elif mutation == "base_manifest_sha":
        base["manifest_sha256"] = "bad"
    else:
        base["manifest_version_id"] = "null"
    with pytest.raises(fresh.FreshRfqError) as error:
        fresh.build_overlay_manifest(
            authority=auth, base_release=base, hour_receipts=receipts)
    assert error.value.code == code


def test_overlay_requires_all_26_hours_resolved_exact(monkeypatch):
    auth, receipts, base = overlay_inputs(monkeypatch)
    hour = receipts[7]["segment_hour"]
    seg = segment(auth, hour)
    receipts[7] = fresh.build_hour_receipt(
        authority=auth, segment_receipt=seg,
        exact_inventory=inventory(seg), raw_key_prefix="ec2/raw")
    assert receipts[7]["resolution_state"] == "NOT_REQUESTED"
    with pytest.raises(fresh.FreshRfqError) as error:
        fresh.build_overlay_manifest(
            authority=auth, base_release=base, hour_receipts=receipts)
    assert error.value.code == "OVERLAY_RESOLUTION"


@pytest.mark.parametrize("mutation", [
    "first_missing_ack", "pid_changes_same_generation",
    "generation_gap", "supervisor_changes",
])
def test_overlay_rejects_broken_persistent_subscription_chain(
        monkeypatch, mutation):
    auth, receipts, base = overlay_inputs(monkeypatch)
    index = 0 if mutation == "first_missing_ack" else 5
    hour = receipts[index]["segment_hour"]
    kwargs = {"subscription_acks": 0, "subscription_end": None}
    if mutation == "pid_changes_same_generation":
        kwargs["child_pid"] = 9999
    elif mutation == "generation_gap":
        kwargs.update(child_pid=9999, child_generation=5,
                      subscription_acks=1, subscription_end=True)
    elif mutation == "supervisor_changes":
        kwargs["supervisor_pid"] = 9999
    seg = segment(auth, hour, **kwargs)
    receipts[index] = fresh.build_hour_receipt(
        authority=auth, segment_receipt=seg,
        exact_inventory=inventory(seg), raw_key_prefix="ec2/raw",
        resolver=lambda row: row)
    with pytest.raises(fresh.FreshRfqError) as error:
        fresh.build_overlay_manifest(
            authority=auth, base_release=base, hour_receipts=receipts)
    assert error.value.code == "SUBSCRIPTION_CHAIN"


def test_overlay_accepts_contiguous_generation_rollover_with_new_ack(monkeypatch):
    auth, receipts, base = overlay_inputs(monkeypatch)
    for index in range(13, 26):
        hour = receipts[index]["segment_hour"]
        seg = segment(
            auth, hour, child_generation=4, child_pid=4300,
            subscription_acks=1 if index == 13 else 0,
            subscription_end=True if index == 13 else None)
        receipts[index] = fresh.build_hour_receipt(
            authority=auth, segment_receipt=seg,
            exact_inventory=inventory(seg), raw_key_prefix="ec2/raw",
            resolver=lambda row: row)
    built = fresh.build_overlay_manifest(
        authority=auth, base_release=base, hour_receipts=receipts)
    assert built["analysis_hours"][13]["child_generation"] == 4
    assert built["analysis_hours"][13]["subscription_ack_count"] == 1
    assert built["watermark_hours"][0]["subscription_ack_count"] == 0


def test_overlay_digest_and_watermark_partition_tamper_fail(monkeypatch):
    auth, receipts, base = overlay_inputs(monkeypatch)
    built = fresh.build_overlay_manifest(
        authority=auth, base_release=base, hour_receipts=receipts)
    tampered = copy.deepcopy(built)
    tampered["manifest_sha256"] = "0" * 64
    with pytest.raises(fresh.FreshRfqError) as error:
        fresh.validate_overlay_manifest(tampered, auth)
    assert error.value.code == "OVERLAY_DIGEST"

    moved = copy.deepcopy(built)
    moved["analysis_hours"].append(moved["watermark_hours"].pop(0))
    with pytest.raises(fresh.FreshRfqError):
        fresh.validate_overlay_manifest(moved, auth)

    base_tamper = copy.deepcopy(built)
    base_tamper["base_release"]["manifest_sha256"] = sha("other manifest")
    base_tamper["manifest_sha256"] = fresh.canonical_sha256({
        key: value for key, value in base_tamper.items()
        if key != "manifest_sha256"
    })
    with pytest.raises(fresh.FreshRfqError, match="BASE_RELEASE_INVALID"):
        fresh.validate_overlay_manifest(base_tamper, auth)
