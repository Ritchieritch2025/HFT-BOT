#!/usr/bin/env python3
"""Exact-body tests for fresh RFQ request provenance."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import fresh_rfq_request_provenance as provenance  # noqa: E402
import fresh_rfq_market_mapping as market_mapping  # noqa: E402


DATE = "2026-07-17"
AUTHORITY_SHA = "a" * 64
SOURCE_EVIDENCE_SHA = "b" * 64
TIME_CONTRACT_SHA = "c" * 64
UTC = dt.timezone.utc


def _wall_ns(hour: int, second: int = 0) -> int:
    when = dt.datetime(2026, 7, 17, hour, 0, second, tzinfo=UTC)
    return int(when.timestamp()) * 1_000_000_000


def _line(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _marker_line(hour: int, marker: str = "hour_open") -> bytes:
    return _line({
        "recv_mono_ns": _wall_ns(hour) - 1,
        "recv_wall_ns": _wall_ns(hour),
        "source": "Kalshi",
        "channel": "",
        "source_ticker": "",
        "marker": marker,
        "raw": "",
    })


def _raw(frame: dict) -> str:
    return json.dumps(frame, separators=(",", ":"))


def _frame_line(hour: int, raw: str, *, second: int = 1,
                channel: str | None = None,
                source_ticker: str | None = None,
                sid: int | None | object = ...) -> bytes:
    parsed = json.loads(raw)
    frame_type = parsed.get("type")
    msg = parsed.get("msg")
    ticker = msg.get("market_ticker") \
        if isinstance(msg, dict) and isinstance(msg.get("market_ticker"), str) \
        else ""
    outer = {
        "recv_mono_ns": _wall_ns(hour, second) - 1,
        "recv_wall_ns": _wall_ns(hour, second),
        "source": "Kalshi",
        "channel": frame_type if channel is None else channel,
        "source_ticker": ticker if source_ticker is None else source_ticker,
        "stream_epoch": 1,
        "raw": raw,
    }
    inner_sid = parsed.get("sid")
    if sid is ...:
        if type(inner_sid) is int and inner_sid >= 0:
            outer["sid"] = inner_sid
    elif sid is not None:
        outer["sid"] = sid
    return _line(outer)


def _created(
    request_id: str,
    market_ticker: str,
    created_ts: str,
    **extra,
) -> str:
    msg = {
        "id": request_id,
        "creator_id": "",
        "market_ticker": market_ticker,
        "created_ts": created_ts,
    }
    msg.update(extra)
    return _raw({"type": "rfq_created", "sid": 17, "msg": msg})


def _happy_rows() -> dict[int, list[bytes]]:
    single = _created(
        "r-single", "KX-SINGLE", "2026-07-17T01:00:10Z",
        contracts_fp="10.25",
    )
    combo = _created(
        "r-combo", "KX-COMBO-TOP", "2026-07-17T12:00:05.123456Z",
        mve_collection_ticker="KX-MVE-COLLECTION",
        mve_selected_legs=[
            {
                "event_ticker": "KX-EVENT-B",
                "market_ticker": "KX-LEG-B",
                "side": "yes",
                "yes_settlement_value_dollars": "1.000000",
            },
            {
                "event_ticker": "KX-EVENT-A",
                "market_ticker": "KX-LEG-A",
                "side": "no",
            },
        ],
        target_cost_dollars="0.000001",
    )
    subscribed = _raw({
        "type": "subscribed",
        "msg": {"channel": "communications", "sid": 17},
    })
    deleted = _raw({
        "type": "rfq_deleted",
        "sid": 17,
        "msg": {
            "id": "r-old",
            "creator_id": "public",
            "market_ticker": "KX-OLD",
            "deleted_ts": "2026-07-17T05:00:01Z",
        },
    })
    return {
        0: [_frame_line(0, subscribed, second=1)],
        1: [
            _frame_line(1, single, second=10),
            _frame_line(1, single, second=11),
        ],
        5: [_frame_line(5, deleted, second=1)],
        12: [_frame_line(12, combo, second=5)],
    }


def _inputs(extra_rows: dict[int, list[bytes]] | None = None) -> dict:
    rows = extra_rows or {}
    identities = []
    exact = []
    for hour in range(24):
        body = _marker_line(hour) + b"".join(rows.get(hour, []))
        key = f"ec2/raw/date={DATE}/rfq_{hour:02d}.ndjson"
        identity = {
            "key": key,
            "version_id": f"version-{hour:02d}",
            "size": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        identities.append(identity)
        exact.append({
            "bucket": provenance.SOURCE_BUCKET,
            **identity,
            "body": body,
        })
    return {
        "analysis_date": DATE,
        "authority_sha256": AUTHORITY_SHA,
        "source_evidence_sha256": SOURCE_EVIDENCE_SHA,
        "time_contract_sha256": TIME_CONTRACT_SHA,
        "analysis_rfq_objects": identities,
        "exact_analysis_rfq_objects": exact,
    }


def _build(inputs: dict | None = None) -> dict:
    return provenance.build_request_provenance(**(inputs or _inputs(_happy_rows())))


def _validate(value: dict, inputs: dict) -> dict:
    return provenance.validate_request_provenance(value, **inputs)


def _assert_code(code: str, callable_, *args, **kwargs) -> None:
    with pytest.raises(provenance.FreshRfqRequestProvenanceError) as exc_info:
        callable_(*args, **kwargs)
    assert exc_info.value.code == code


def _replace_body(inputs: dict, hour: int, body: bytes, *,
                  refresh_identity: bool) -> None:
    key = f"ec2/raw/date={DATE}/rfq_{hour:02d}.ndjson"
    exact = next(row for row in inputs["exact_analysis_rfq_objects"]
                 if row["key"] == key)
    exact["body"] = body
    if refresh_identity:
        size = len(body)
        digest = hashlib.sha256(body).hexdigest()
        exact["size"] = size
        exact["sha256"] = digest
        overlay = next(row for row in inputs["analysis_rfq_objects"]
                       if row["key"] == key)
        overlay["size"] = size
        overlay["sha256"] = digest


def _reseal(value: dict) -> None:
    unsigned = copy.deepcopy(value)
    unsigned.pop("receipt_sha256")
    value["receipt_sha256"] = provenance.canonical_sha256(unsigned)


def _contains_bytes(value) -> bool:
    if isinstance(value, bytes):
        return True
    if isinstance(value, dict):
        return any(_contains_bytes(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_bytes(item) for item in value)
    return False


def test_all_objects_bytes_rows_requests_and_exact_duplicates_are_conserved() -> None:
    inputs = _inputs(_happy_rows())
    result = _build(inputs)

    assert result["schema"] == "fresh-rfq-request-provenance-v1"
    assert result["analysis_rfq_object_count"] == 24
    assert result["analysis_rfq_total_bytes"] == sum(
        row["size"] for row in inputs["analysis_rfq_objects"]
    )
    assert all(row["parsed_bytes"] == row["size"]
               for row in result["object_coverage"])
    assert result["physical_line_count"] == 29
    assert result["marker_row_count"] == 24
    assert result["irrelevant_frame_row_count"] == 2
    assert result["rfq_created_occurrence_count"] == 3
    assert result["rfq_created_unique_count"] == 2
    assert result["rfq_created_exact_duplicate_count"] == 1
    assert result["physical_line_count"] == (
        result["marker_row_count"]
        + result["irrelevant_frame_row_count"]
        + result["rfq_created_occurrence_count"]
    )
    assert result["rfq_created_occurrence_count"] == (
        result["rfq_created_unique_count"]
        + result["rfq_created_exact_duplicate_count"]
    )
    assert [row["request_id"] for row in result["rfq_requests"]] == [
        "r-combo", "r-single",
    ]
    assert result["rfq_requests"][0]["mve_selected_legs"] == [
        {"market_ticker": "KX-LEG-A"},
        {"market_ticker": "KX-LEG-B"},
    ]
    duplicate = result["exact_duplicate_ledger"][0]
    assert duplicate["request_id"] == "r-single"
    assert duplicate["occurrence_kind"] == "EXACT_RAW_DUPLICATE"
    assert duplicate["line_number"] != duplicate["primary_line_number"]
    assert result["rfq_input_sha256"] == provenance.canonical_sha256(
        result["rfq_requests"]
    )
    assert result["analysis_rfq_object_set_sha256"] == \
        provenance.canonical_sha256(result["analysis_rfq_objects"])
    assert result["source_objects_exact_get_verified"] is False
    assert result["aws_read_performed_by_module"] is False
    assert result["aws_write_authorized"] is False
    assert result["research_eligible"] is False
    assert result["research_ready"] is False
    assert result["input_bodies_omitted"] is True
    assert _contains_bytes(result) is False
    assert _validate(result, inputs) == result


def test_zero_created_requests_is_valid_and_fully_classified() -> None:
    subscribed = _raw({
        "type": "subscribed",
        "msg": {"channel": "communications", "sid": 1},
    })
    inputs = _inputs({0: [_frame_line(0, subscribed)]})
    result = _build(inputs)
    assert result["rfq_requests"] == []
    assert result["rfq_created_occurrence_count"] == 0
    assert result["rfq_created_unique_count"] == 0
    assert result["rfq_input_sha256"] == provenance.canonical_sha256([])
    assert result["physical_line_count"] == 25
    assert result["all_unique_created_projected"] is True


def test_projected_requests_are_exactly_accepted_by_current_mapper() -> None:
    result = _build()
    mapped = market_mapping.build_market_mapping(
        analysis_date=DATE,
        base_binding_sha256="d" * 64,
        analysis_rfq_object_set_sha256=(
            result["analysis_rfq_object_set_sha256"]
        ),
        time_contract_sha256=TIME_CONTRACT_SHA,
        rfq_requests=result["rfq_requests"],
        l1_market_universe=[],
        l2_market_universe=[],
        pre_event_window_ms=0,
        post_event_window_ms=0,
    )
    assert mapped["rfq_input_sha256"] == result["rfq_input_sha256"]
    assert mapped["rfq_request_count"] == result["rfq_created_unique_count"]


def test_exact_object_set_rejects_missing_extra_and_shard_gap() -> None:
    missing = _inputs()
    missing["exact_analysis_rfq_objects"].pop()
    _assert_code("OBJECT_SET_MISMATCH", _build, missing)

    extra = _inputs()
    original = extra["exact_analysis_rfq_objects"][0]
    extra["exact_analysis_rfq_objects"].append({
        **copy.deepcopy(original),
        "key": f"ec2/raw/date={DATE}/rfq_00.ndjson.1",
        "version_id": "extra-version",
    })
    _assert_code("OBJECT_SET_MISMATCH", _build, extra)

    shard_gap = _inputs()
    row = shard_gap["analysis_rfq_objects"][0]
    exact_row = shard_gap["exact_analysis_rfq_objects"][0]
    row["key"] = f"ec2/raw/date={DATE}/rfq_00.ndjson.2"
    exact_row["key"] = row["key"]
    _assert_code("OBJECT_SHARD_CONTINUITY", _build, shard_gap)


def test_two_shards_parse_in_natural_order_and_input_order_is_irrelevant() -> None:
    inputs = _inputs()
    hour = 7
    key = f"ec2/raw/date={DATE}/rfq_{hour:02d}.ndjson.1"
    created = _created("r-shard", "KX-SHARD", "2026-07-17T07:00:02Z")
    body = _marker_line(hour, "shard_open") + _frame_line(
        hour, created, second=2,
    )
    identity = {
        "key": key,
        "version_id": "version-07-1",
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    inputs["analysis_rfq_objects"].append(identity)
    inputs["exact_analysis_rfq_objects"].append({
        "bucket": provenance.SOURCE_BUCKET,
        **identity,
        "body": body,
    })

    ordered = _build(inputs)
    shuffled = copy.deepcopy(inputs)
    shuffled["analysis_rfq_objects"].reverse()
    shuffled["exact_analysis_rfq_objects"].reverse()
    rebuilt = _build(shuffled)

    assert rebuilt == ordered
    assert ordered["analysis_rfq_object_count"] == 25
    hour_coverage = [
        row for row in ordered["object_coverage"]
        if row["key"].startswith(f"ec2/raw/date={DATE}/rfq_07.ndjson")
    ]
    assert [row["key"] for row in hour_coverage] == [
        f"ec2/raw/date={DATE}/rfq_07.ndjson",
        f"ec2/raw/date={DATE}/rfq_07.ndjson.1",
    ]
    assert [row["physical_line_count"] for row in hour_coverage] == [1, 2]
    assert all(row["parsed_bytes"] == row["size"] for row in hour_coverage)
    assert ordered["physical_line_count"] == 26
    assert ordered["rfq_requests"] == [{
        "request_id": "r-shard",
        "created_ts": "2026-07-17T07:00:02Z",
        "market_ticker": "KX-SHARD",
        "mve_collection_ticker": None,
        "mve_selected_legs": [],
    }]


@pytest.mark.parametrize("field", ["version_id", "sha256"])
def test_exact_object_identity_must_equal_overlay_identity(field: str) -> None:
    inputs = _inputs()
    exact = inputs["exact_analysis_rfq_objects"][0]
    exact[field] = "different-version" if field == "version_id" else "d" * 64
    _assert_code("OBJECT_IDENTITY_MISMATCH", _build, inputs)


def test_same_size_different_exact_body_cannot_retain_overlay_sha() -> None:
    inputs = _inputs()
    exact = inputs["exact_analysis_rfq_objects"][0]
    body = exact["body"]
    replacement = b"X" if body[0:1] != b"X" else b"Y"
    exact["body"] = replacement + body[1:]
    assert len(exact["body"]) == exact["size"]
    _assert_code("OBJECT_SHA_MISMATCH", _build, inputs)


@pytest.mark.parametrize("change", ["delete", "append"])
def test_line_deletion_or_addition_cannot_retain_object_identity(change: str) -> None:
    inputs = _inputs()
    body = inputs["exact_analysis_rfq_objects"][0]["body"]
    changed = body[:-1] if change == "delete" else body + _marker_line(0, "extra")
    _replace_body(inputs, 0, changed, refresh_identity=False)
    _assert_code("OBJECT_SIZE_MISMATCH", _build, inputs)


def test_torn_and_crlf_rows_fail_after_valid_identity_rebinding() -> None:
    torn = _inputs()
    body = torn["exact_analysis_rfq_objects"][0]["body"][:-1]
    _replace_body(torn, 0, body, refresh_identity=True)
    _assert_code("UNTERMINATED_LINE", _build, torn)

    crlf = _inputs()
    body = crlf["exact_analysis_rfq_objects"][0]["body"].replace(
        b"\n", b"\r\n", 1,
    )
    _replace_body(crlf, 0, body, refresh_identity=True)
    _assert_code("CRLF_FORBIDDEN", _build, crlf)


def test_outer_and_inner_duplicate_json_keys_fail_closed() -> None:
    outer_dup = _inputs()
    body = outer_dup["exact_analysis_rfq_objects"][0]["body"]
    body = body.replace(
        b'{"channel"',
        b'{"recv_mono_ns":1,"recv_mono_ns":2,"channel"',
        1,
    )
    _replace_body(outer_dup, 0, body, refresh_identity=True)
    _assert_code("DUPLICATE_JSON_KEY", _build, outer_dup)

    inner_dup = _inputs()
    raw = (
        '{"type":"rfq_created","type":"rfq_created","sid":17,'
        '"msg":{"id":"r1","creator_id":"","market_ticker":"KX-A",'
        '"created_ts":"2026-07-17T00:00:01Z"}}'
    )
    outer = {
        "recv_mono_ns": _wall_ns(0, 1) - 1,
        "recv_wall_ns": _wall_ns(0, 1),
        "source": "Kalshi",
        "channel": "rfq_created",
        "source_ticker": "KX-A",
        "sid": 17,
        "stream_epoch": 1,
        "raw": raw,
    }
    _replace_body(
        inner_dup, 0, _marker_line(0) + _line(outer), refresh_identity=True,
    )
    _assert_code("DUPLICATE_JSON_KEY", _build, inner_dup)


def test_raw_b64_and_outer_inner_mismatch_fail_closed() -> None:
    raw_b64 = _inputs()
    outer = {
        "recv_mono_ns": _wall_ns(0, 1) - 1,
        "recv_wall_ns": _wall_ns(0, 1),
        "source": "Kalshi",
        "channel": "binary",
        "source_ticker": "",
        "raw_b64": "//4=",
    }
    _replace_body(raw_b64, 0, _marker_line(0) + _line(outer),
                  refresh_identity=True)
    _assert_code("RAW_B64_FORBIDDEN", _build, raw_b64)

    mismatch = _inputs()
    created = _created("r1", "KX-A", "2026-07-17T00:00:01Z")
    bad = _frame_line(0, created, channel="rfq_deleted")
    _replace_body(mismatch, 0, _marker_line(0) + bad, refresh_identity=True)
    _assert_code("ENVELOPE_FRAME_MISMATCH", _build, mismatch)


def test_combo_invalid_cross_date_and_same_id_different_raw_fail() -> None:
    invalid_combo = _inputs()
    raw = _created(
        "r1", "KX-COMBO", "2026-07-17T00:00:01Z",
        mve_collection_ticker="KX-COLLECTION",
        mve_selected_legs=[{"side": "yes"}],
    )
    _replace_body(
        invalid_combo, 0, _marker_line(0) + _frame_line(0, raw),
        refresh_identity=True,
    )
    _assert_code("RFQ_COMBO_INVALID", _build, invalid_combo)

    cross_date = _inputs()
    raw = _created("r1", "KX-A", "2026-07-16T23:59:59Z")
    _replace_body(
        cross_date, 0, _marker_line(0) + _frame_line(0, raw),
        refresh_identity=True,
    )
    _assert_code("RFQ_CREATED_CROSS_DATE", _build, cross_date)

    collision = _inputs()
    first = _created("r1", "KX-A", "2026-07-17T01:00:01Z")
    second = _created("r1", "KX-B", "2026-07-17T01:00:01Z")
    _replace_body(
        collision,
        1,
        _marker_line(1)
        + _frame_line(1, first, second=1)
        + _frame_line(1, second, second=2),
        refresh_identity=True,
    )
    _assert_code("RFQ_ID_COLLISION", _build, collision)


def test_invalid_relevant_frame_never_becomes_irrelevant() -> None:
    inputs = _inputs()
    raw = _raw({
        "type": "rfq_created",
        "sid": 17,
        "msg": {
            "id": "r1",
            "creator_id": "",
            "created_ts": "2026-07-17T00:00:01Z",
        },
    })
    _replace_body(
        inputs, 0, _marker_line(0) + _frame_line(0, raw),
        refresh_identity=True,
    )
    _assert_code("RFQ_CREATED_INVALID", _build, inputs)


@pytest.mark.parametrize("field,value", [
    ("sid", True),
    ("sid", -1),
    ("seq", "1"),
    ("seq", 1 << 64),
])
def test_irrelevant_frame_rejects_present_non_uint64_sid_or_seq(
    field: str, value,
) -> None:
    inputs = _inputs()
    frame = {"type": "quote_created", "msg": {"id": "q1"}, field: value}
    raw = _raw(frame)
    # ws_client would not project an invalid uint64 into the outer envelope.
    row = _frame_line(0, raw, sid=None)
    _replace_body(
        inputs, 0, _marker_line(0) + row, refresh_identity=True,
    )
    _assert_code("INNER_FRAME_INVALID", _build, inputs)


def test_validator_rebuild_rejects_tamper_even_after_receipt_rehash() -> None:
    inputs = _inputs(_happy_rows())
    result = _build(inputs)
    tampered = copy.deepcopy(result)
    tampered["rfq_requests"][0]["market_ticker"] = "KX-TAMPERED"
    tampered["rfq_input_sha256"] = provenance.canonical_sha256(
        tampered["rfq_requests"]
    )
    _reseal(tampered)
    _assert_code(
        "PROVENANCE_REBUILD_MISMATCH", _validate, tampered, inputs,
    )


def test_validator_binds_original_exact_bodies_and_all_control_shas() -> None:
    inputs = _inputs(_happy_rows())
    result = _build(inputs)

    changed = copy.deepcopy(inputs)
    original = changed["exact_analysis_rfq_objects"][2]["body"]
    _replace_body(
        changed, 2, original + _marker_line(2, "extra"),
        refresh_identity=True,
    )
    _assert_code("PROVENANCE_REBUILD_MISMATCH", _validate, result, changed)

    for field in (
        "authority_sha256", "source_evidence_sha256", "time_contract_sha256",
    ):
        wrong = copy.deepcopy(inputs)
        wrong[field] = "d" * 64
        _assert_code("PROVENANCE_REBUILD_MISMATCH", _validate, result, wrong)
