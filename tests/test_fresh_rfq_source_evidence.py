"""Offline exact-byte tests for fresh RFQ source evidence."""

import copy
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "fresh_rfq_source_evidence.py"
UTC = dt.timezone.utc
DATE = "2026-07-17"
NEXT_DATE = "2026-07-18"
AUTHORITY_SHA = hashlib.sha256(b"authority").hexdigest()
GENERATION = "fresh-rfq-generation-17"


def load_module():
    spec = importlib.util.spec_from_file_location("fresh_rfq_source_evidence", TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


source = load_module()


def sha(body):
    return hashlib.sha256(body).hexdigest()


def exact_object(key, body, *, version=None):
    return {
        "bucket": source.SOURCE_BUCKET,
        "key": key,
        "version_id": version or "version-" + sha(key.encode())[:16],
        "size": len(body),
        "sha256": sha(body),
        "body": body,
    }


def identity(obj):
    return {key: obj[key] for key in (
        "bucket", "key", "version_id", "size", "sha256")}


def raw_file(relpath, body):
    return {
        "file": relpath,
        "size": len(body),
        "inode": 1,
        "mtime_ns": 1,
        "ctime_ns": 1,
        "checkpoint": len(body),
        "sha256": sha(body),
    }


def expected_hours():
    start = dt.datetime(2026, 7, 17, tzinfo=UTC)
    return [(start + dt.timedelta(hours=index)).strftime("%Y-%m-%dT%H")
            for index in range(26)]


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    )


def capture_rows_for_hour(capture_objects, hour):
    prefix = "ec2/raw/date=%s/rfq_%s.ndjson" % tuple(hour.split("T"))
    rows = [obj for obj in capture_objects
            if obj["key"] == prefix or obj["key"].startswith(prefix + ".")]

    def ordinal(obj):
        suffix = obj["key"][len(prefix):]
        return 0 if not suffix else int(suffix[1:])

    return sorted(rows, key=ordinal)


def segment_receipt(hour, capture_objects, *, status="PASS"):
    objects = capture_rows_for_hour(capture_objects, hour)
    shards = []
    for ordinal, obj in enumerate(objects):
        relpath = obj["key"][len("ec2/raw/"):]
        shards.append({
            "ordinal": ordinal,
            "relpath": relpath,
            "bytes_before": 0,
            "size": obj["size"],
            "parsed_bytes_at_close": obj["size"],
            "sha256": obj["sha256"],
        })
    base_rel = "date=%s/rfq_%s.ndjson" % tuple(hour.split("T"))
    return {
        "type": "rfq_segment_receipt",
        "schema": "rfq-segment-receipt-v3",
        "segment_hour": hour,
        "status": status,
        "findings": [],
        "fresh_lane_state": "BOUND_AUTHORITY",
        "authority_sha256": AUTHORITY_SHA,
        "generation": GENERATION,
        "capture_relpath": base_rel,
        "capture_bytes_before": 0,
        "capture_bytes_at_close": sum(row["size"] for row in shards),
        "capture_sha256_at_close": shards[0]["sha256"]
        if len(shards) == 1 else None,
        "capture_shards": shards,
        "capture_shard_count": len(shards),
        "capture_shard_set_sha256": source.canonical_sha256(shards),
    }


def outer_line(receipt, close_hour, *, raw_override=None):
    close = dt.datetime.strptime(close_hour, "%Y-%m-%dT%H").replace(tzinfo=UTC)
    outer = {
        "recv_mono_ns": 123456789,
        "recv_wall_ns": int(close.timestamp() * 1_000_000_000) + 123,
        "source": "Kalshi",
        "channel": "rfq_segment_receipt",
        "source_ticker": "",
        "marker": "segment_receipt",
        "raw": canonical(receipt) if raw_override is None else raw_override,
    }
    return (canonical(outer) + "\n").encode()


def make_seal(raw_files):
    return {
        "version": 2,
        "method": "full_v2",
        "go_no_go_eligible": True,
        "unverified": [],
        "code_commit": "a" * 40,
        "status": "SEALED",
        "date": DATE,
        "sealed_at": "2026-07-18T02:05:00Z",
        "manifest_date_sha256": sha(b"manifest"),
        "archive_files": 0,
        "archive_rows": 0,
        "archive_file_stats": [],
        "capture_quality_status": "UNASSESSED_PENDING_PIPE_W03",
        "raw_retention_requirement": "LOCAL_OR_VAULT_VERIFIED_RECEIPT",
        "receipt_cross_day_hours": 2,
        "raw_files": raw_files,
        "discovery_completeness": {},
    }


def reseal(case, seal):
    body = (canonical(seal) + "\n").encode()
    case["seal_object"] = exact_object(
        f"ec2/warehouse/seals/date={DATE}.json", body,
        version="seal-version-1")
    case["_seal"] = seal


def replace_container(case, key, body, *, version=None):
    replacement = exact_object(key, body, version=version)
    for index, obj in enumerate(case["receipt_container_objects"]):
        if obj["key"] == key:
            case["receipt_container_objects"][index] = replacement
            break
    else:
        raise AssertionError("container not found")
    for index, row in enumerate(case["complete_container_identities"]):
        if row["key"] == key:
            case["complete_container_identities"][index] = identity(replacement)
            break
    else:
        raise AssertionError("container identity not found")
    return replacement


def build_case(*, shard_counts=None):
    hours = expected_hours()
    shard_counts = shard_counts or {}
    captures = []
    capture_raw = []
    for hour in hours:
        count = shard_counts.get(hour, 1)
        date_text, hh = hour.split("T")
        for ordinal in range(count):
            relpath = f"date={date_text}/rfq_{hh}.ndjson"
            if ordinal:
                relpath += f".{ordinal}"
            body = (canonical({"hour": hour, "ordinal": ordinal}) + "\n").encode()
            obj = exact_object("ec2/raw/" + relpath, body)
            captures.append(obj)
            capture_raw.append(raw_file(relpath, body))

    containers = []
    container_raw = []
    for index, hour in enumerate(hours):
        close = (dt.datetime.strptime(hour, "%Y-%m-%dT%H").replace(tzinfo=UTC) +
                 dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H")
        close_date, close_hh = close.split("T")
        relpath = f"date={close_date}/rfq_receipts_{close_hh}.ndjson"
        receipt = segment_receipt(hour, captures)
        body = outer_line(receipt, close)
        obj = exact_object("ec2/raw/" + relpath, body)
        containers.append(obj)
        if index < 25:
            container_raw.append(raw_file(relpath, body))

    # Non-RFQ raw proves classification is exact, not a generic kind filter.
    other = b'{"channel":"ticker"}\n'
    seal = make_seal(
        list(reversed(capture_raw)) + list(reversed(container_raw)) +
        [raw_file(f"date={DATE}/firehose_00.ndjson", other)])
    case = {
        "seal_object": None,
        "capture_objects": list(reversed(captures)),
        "receipt_container_objects": list(reversed(containers)),
        "complete_container_identities": [identity(row)
                                            for row in reversed(containers)],
        "expected_hours": hours,
        "authority_sha256": AUTHORITY_SHA,
        "generation": GENERATION,
        "_seal": seal,
    }
    reseal(case, seal)
    return case


def build(case):
    return source.build_source_evidence(**{
        key: value for key, value in case.items() if not key.startswith("_")
    })


def error_code(case):
    with pytest.raises(source.SourceEvidenceError) as error:
        build(case)
    return error.value.code


def test_exact_source_evidence_binds_seal_types_lines_and_late_02():
    case = build_case()
    result = build(case)

    assert result["schema"] == "fresh-rfq-source-evidence-v1"
    assert result["state"] == "ALL_INPUT_EXACT_VERSION_BYTES_VERIFIED"
    assert result["analysis_date"] == DATE
    assert len(result["analysis_capture_objects"]) == 24
    assert len(result["watermark_capture_objects"]) == 2
    assert len(result["analysis_receipt_proofs"]) == 24
    assert len(result["watermark_receipt_proofs"]) == 2
    assert all(row["role"] == "ANALYSIS"
               for row in result["analysis_capture_objects"])
    assert all(row["role"] == "WATERMARK"
               for row in result["watermark_capture_objects"])
    # full_v2 physically seals two cross-day receipt-time capture hours. They
    # remain watermark-only and are never counted in D's analysis set.
    assert len(result["seal_rfq_capture_members"]) == 26
    assert all(row["seal_member"] is True
               for row in result["watermark_capture_objects"])
    assert all(row["seal_membership_scope"] ==
               "D_FULL_V2_CROSS_DAY_MEMBER"
               for row in result["watermark_capture_objects"])
    assert {row["source_hour"] for row in result["analysis_capture_objects"]} == \
        set(case["expected_hours"][:24])
    assert {row["source_hour"] for row in result["watermark_capture_objects"]} == \
        set(case["expected_hours"][24:])

    final = result["watermark_receipt_proofs"][-1]
    assert final["segment_hour"] == f"{NEXT_DATE}T01"
    assert final["container_key"].endswith(
        f"date={NEXT_DATE}/rfq_receipts_02.ndjson")
    assert final["container_seal_member"] is False
    assert final["line_number"] == 1
    assert final["byte_offset"] == 0
    assert final["byte_length"] == next(
        row["size"] for row in result["receipt_containers"]
        if row["key"] == final["container_key"])
    assert len(final["outer_row_sha256"]) == 64
    assert final["outer_row_sha256"] == final["container_sha256"]
    assert len(final["raw_segment_canonical_sha256"]) == 64
    assert result["all_input_bodies_omitted_from_output"] is True
    assert '"body"' not in canonical(result)


def test_natural_capture_shard_order_is_independent_of_input_and_seal_order():
    hour = f"{DATE}T00"
    case = build_case(shard_counts={hour: 3})
    result = build(case)
    first = [row for row in result["analysis_capture_objects"]
             if row["source_hour"] == hour]
    assert [row["ordinal"] for row in first] == [0, 1, 2]
    assert [row["relpath"] for row in first] == [
        f"date={DATE}/rfq_00.ndjson",
        f"date={DATE}/rfq_00.ndjson.1",
        f"date={DATE}/rfq_00.ndjson.2",
    ]


def test_complete_identity_set_prevents_cherry_pick_of_omitted_fail_container():
    case = build_case()
    final_key = f"ec2/raw/date={NEXT_DATE}/rfq_receipts_02.ndjson"
    fail_key = final_key + ".1"
    fail_receipt = segment_receipt(
        f"{NEXT_DATE}T01", case["capture_objects"], status="FAIL")
    fail_obj = exact_object(
        fail_key, outer_line(fail_receipt, f"{NEXT_DATE}T02"))
    # Complete set evidence names the FAIL shard, while a cherry-picked body
    # list omits it. The parser fails before selecting the PASS line.
    case["complete_container_identities"].append(identity(fail_obj))
    assert error_code(case) == "CONTAINER_SET_MISMATCH"


def test_same_hour_pass_and_fail_lines_are_ambiguous_not_superseded():
    case = build_case()
    fail_receipt = segment_receipt(
        f"{NEXT_DATE}T01", case["capture_objects"], status="FAIL")
    fail_obj = exact_object(
        f"ec2/raw/date={NEXT_DATE}/rfq_receipts_02.ndjson.1",
        outer_line(fail_receipt, f"{NEXT_DATE}T02"))
    case["receipt_container_objects"].append(fail_obj)
    case["complete_container_identities"].append(identity(fail_obj))
    assert error_code(case) == "RECEIPT_AMBIGUOUS"


@pytest.mark.parametrize("mutation,expected", [
    ("torn", "TORN_CONTAINER"),
    ("malformed", "INVALID_JSON"),
])
def test_malformed_or_torn_physical_container_line_fails(mutation, expected):
    case = build_case()
    key = f"ec2/raw/date={NEXT_DATE}/rfq_receipts_02.ndjson"
    original = next(row for row in case["receipt_container_objects"]
                    if row["key"] == key)["body"]
    body = original[:-1] if mutation == "torn" else b"{not-json}\n"
    replace_container(case, key, body)
    assert error_code(case) == expected


@pytest.mark.parametrize("mutation,expected", [
    ("sha", "OBJECT_SHA_MISMATCH"),
    ("version", "VERSION_REQUIRED"),
])
def test_container_body_sha_and_non_null_version_are_mandatory(mutation, expected):
    case = build_case()
    key = f"ec2/raw/date={NEXT_DATE}/rfq_receipts_02.ndjson"
    obj = next(row for row in case["receipt_container_objects"]
               if row["key"] == key)
    if mutation == "sha":
        obj["sha256"] = "0" * 64
    else:
        obj["version_id"] = "null"
    assert error_code(case) == expected


def test_receipt_and_capture_object_lists_cannot_be_mixed():
    case = build_case()
    case["receipt_container_objects"][0] = case["capture_objects"][0]
    assert error_code(case) == "SOURCE_KIND_MIX"


def test_post_receipt_extra_line_is_not_silently_ignored():
    case = build_case()
    key = f"ec2/raw/date={NEXT_DATE}/rfq_receipts_02.ndjson"
    original = next(row for row in case["receipt_container_objects"]
                    if row["key"] == key)["body"]
    extra = segment_receipt(f"{NEXT_DATE}T01", case["capture_objects"])
    extra["segment_hour"] = f"{NEXT_DATE}T02"
    body = original + outer_line(extra, f"{NEXT_DATE}T02")
    replace_container(case, key, body)
    assert error_code(case) == "POST_RECEIPT_EXTRA"


def test_duplicate_keys_fail_in_seal_and_inner_segment():
    case = build_case()
    seal_body = case["seal_object"]["body"].replace(
        b'"method":"full_v2"',
        b'"method":"full_v2","method":"full_v2"', 1)
    case["seal_object"] = exact_object(
        f"ec2/warehouse/seals/date={DATE}.json", seal_body,
        version="seal-version-duplicate")
    assert error_code(case) == "DUPLICATE_JSON_KEY"

    case = build_case()
    key = f"ec2/raw/date={NEXT_DATE}/rfq_receipts_02.ndjson"
    receipt = segment_receipt(f"{NEXT_DATE}T01", case["capture_objects"])
    raw = canonical(receipt).replace(
        '"status":"PASS"', '"status":"PASS","status":"FAIL"', 1)
    replace_container(case, key, outer_line(
        receipt, f"{NEXT_DATE}T02", raw_override=raw))
    assert error_code(case) == "DUPLICATE_JSON_KEY"


def test_capture_or_receipt_family_with_noncanonical_suffix_is_rejected():
    case = build_case()
    seal = copy.deepcopy(case["_seal"])
    body = b"x\n"
    seal["raw_files"].append(raw_file(
        f"date={DATE}/rfq_receipts_03.ndjson.bad", body))
    reseal(case, seal)
    assert error_code(case) == "RFQ_KIND_MIX"


def test_seal_exact_identity_and_capture_body_are_verified():
    case = build_case()
    case["seal_object"]["key"] = (
        "ec2/warehouse/seals/date=2026-07-16.json")
    assert error_code(case) == "SEAL_IDENTITY"

    case = build_case()
    case["capture_objects"][0]["body"] += b"post-seal-extra\n"
    assert error_code(case) == "OBJECT_SIZE_MISMATCH"
