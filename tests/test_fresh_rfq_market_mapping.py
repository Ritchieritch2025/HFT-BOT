#!/usr/bin/env python3
"""Pure tests for deterministic fresh-RFQ market mapping."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import fresh_rfq_market_mapping as mapping  # noqa: E402


DATE = "2026-07-17"
BASE_SHA = "a" * 64
RFQ_OBJECT_SET_SHA = "b" * 64
TIME_CONTRACT_SHA = "c" * 64


def _request(
    request_id: str,
    market_ticker: str,
    *,
    created_ts: str = "2026-07-17T12:00:00Z",
    collection_ticker: str | None = None,
    legs: list[str] | None = None,
) -> dict:
    return {
        "request_id": request_id,
        "created_ts": created_ts,
        "market_ticker": market_ticker,
        "mve_collection_ticker": collection_ticker,
        "mve_selected_legs": [
            {"market_ticker": ticker} for ticker in (legs or [])
        ],
    }


def _universe(*tickers: str, date: str = DATE) -> list[dict]:
    return [
        {"analysis_date": date, "market_ticker": ticker}
        for ticker in tickers
    ]


def _inputs() -> dict:
    return {
        "analysis_date": DATE,
        "base_binding_sha256": BASE_SHA,
        "analysis_rfq_object_set_sha256": RFQ_OBJECT_SET_SHA,
        "time_contract_sha256": TIME_CONTRACT_SHA,
        "rfq_requests": [
            _request("r-missing", "KX-NONE", created_ts="2026-07-17T12:03:00Z"),
            _request(
                "r-combo",
                "KX-COMBO-TOP",
                created_ts="2026-07-17T12:02:00Z",
                collection_ticker="KX-MVE-COLLECTION",
                legs=["KX-L2-ONLY", "KX-L1-ONLY"],
            ),
            _request("r-single", "KX-BOTH", created_ts="2026-07-17T12:01:00Z"),
        ],
        "l1_market_universe": _universe("KX-L1-ONLY", "KX-BOTH"),
        "l2_market_universe": _universe("KX-L2-ONLY", "KX-BOTH"),
        "pre_event_window_ms": 60_000,
        "post_event_window_ms": 120_000,
    }


def _build(**overrides) -> dict:
    inputs = _inputs()
    inputs.update(overrides)
    return mapping.build_market_mapping(**inputs)


def _validate(value: dict, **overrides) -> dict:
    inputs = _inputs()
    inputs.update(overrides)
    return mapping.validate_market_mapping(value, **inputs)


def _reseal(value: dict) -> None:
    value["mapping_row_set_sha256"] = mapping.canonical_sha256(
        value["mapping_rows"]
    )
    value["dq_ledger_sha256"] = mapping.canonical_sha256(value["dq_ledger"])
    value["ignored_combo_top_level_sha256"] = mapping.canonical_sha256(
        value["ignored_combo_top_levels"]
    )
    unsigned = copy.deepcopy(value)
    unsigned.pop("mapping_sha256")
    value["mapping_sha256"] = mapping.canonical_sha256(unsigned)


def _assert_code(code: str, callable_, *args, **kwargs) -> None:
    with pytest.raises(mapping.FreshRfqMarketMappingError) as exc_info:
        callable_(*args, **kwargs)
    assert exc_info.value.code == code


def test_exact_single_combo_mapping_and_deterministic_dq() -> None:
    result = _build()

    rows = result["mapping_rows"]
    assert [
        (row["request_id"], row["component_index"], row["market_ticker"])
        for row in rows
    ] == [
        ("r-combo", 0, "KX-L1-ONLY"),
        ("r-combo", 1, "KX-L2-ONLY"),
        ("r-missing", 0, "KX-NONE"),
        ("r-single", 0, "KX-BOTH"),
    ]
    assert [row["mapping_state"] for row in rows] == [
        "MISSING_L2",
        "MISSING_L1",
        "MISSING_L1_L2",
        "MAPPED_L1_L2",
    ]
    assert all(row["event_window_within_base_date"] is True for row in rows)
    assert {
        (row["request_id"], row["market_ticker"], row["dq_code"])
        for row in result["dq_ledger"]
    } == {
        ("r-combo", "KX-L1-ONLY", "MISSING_L2"),
        ("r-combo", "KX-L2-ONLY", "MISSING_L1"),
        ("r-missing", "KX-NONE", "MISSING_L1_L2"),
    }

    all_mapped_tickers = {row["market_ticker"] for row in rows}
    all_dq_tickers = {row["market_ticker"] for row in result["dq_ledger"]}
    assert "KX-COMBO-TOP" not in all_mapped_tickers
    assert "KX-COMBO-TOP" not in all_dq_tickers
    assert result["ignored_combo_top_levels"] == [{
        "request_id": "r-combo",
        "created_ts": "2026-07-17T12:02:00Z",
        "market_ticker": "KX-COMBO-TOP",
        "audit_reason": "COMBO_TOP_LEVEL_NOT_A_BASE_MARKET_COMPONENT",
    }]
    assert result["ignored_combo_top_level_count"] == 1
    assert result["ignored_combo_top_level_sha256"] == mapping.canonical_sha256(
        result["ignored_combo_top_levels"]
    )

    assert result["base_binding_sha256"] == BASE_SHA
    assert result["analysis_rfq_object_set_sha256"] == RFQ_OBJECT_SET_SHA
    assert result["time_contract_sha256"] == TIME_CONTRACT_SHA
    assert result["analysis_date"] == DATE
    assert result["rfq_request_count"] == 3
    assert result["mapping_input_ticker_count"] == 4
    assert result["dq_count"] == 3
    assert result["all_mapping_components_retained"] is True
    assert result["matching_policy"] == "EXACT_CASE_SENSITIVE_NO_FALLBACK"
    assert result["cross_date_policy"] == "DQ_NO_CROSS_DATE_BORROW"
    assert result["event_window_policy"] == (
        "RETAIN_ROW_DQ_NO_CROSS_DATE_BORROW"
    )
    assert result["event_window_interval_contract"] == (
        "CLOSED_WINDOW_WITHIN_HALF_OPEN_BASE_DATE"
    )
    assert result["state"] == "LOCALLY_DERIVED_UNPUBLISHED_UNVERIFIED"
    assert result["source_objects_exact_get_verified"] is False
    assert result["data_objects_copied"] == 0
    assert result["aws_write_authorized"] is False
    assert result["research_eligible"] is False
    assert result["research_ready"] is False
    assert _validate(result) == result


def test_input_digests_bind_normalized_requests_and_independent_universes() -> None:
    result = _build()
    normalized_requests = sorted(
        copy.deepcopy(_inputs()["rfq_requests"]),
        key=lambda row: row["request_id"],
    )
    for request in normalized_requests:
        request["mve_selected_legs"].sort(
            key=lambda leg: leg["market_ticker"]
        )
    expected_l1 = sorted(
        _inputs()["l1_market_universe"], key=lambda row: row["market_ticker"]
    )
    expected_l2 = sorted(
        _inputs()["l2_market_universe"], key=lambda row: row["market_ticker"]
    )

    assert result["rfq_input_sha256"] == mapping.canonical_sha256(
        normalized_requests
    )
    assert result["l1_market_universe_sha256"] == mapping.canonical_sha256(
        expected_l1
    )
    assert result["l2_market_universe_sha256"] == mapping.canonical_sha256(
        expected_l2
    )
    assert result["l1_market_universe_sha256"] != result[
        "l2_market_universe_sha256"
    ]


def test_permuted_input_and_combo_leg_order_have_identical_output() -> None:
    original = _inputs()
    permuted = copy.deepcopy(original)
    permuted["rfq_requests"].reverse()
    permuted["rfq_requests"][1]["mve_selected_legs"].reverse()
    permuted["l1_market_universe"].reverse()
    permuted["l2_market_universe"].reverse()

    assert mapping.build_market_mapping(**original) == mapping.build_market_mapping(
        **permuted
    )


def test_same_ticker_on_distinct_requests_retains_two_mapping_rows() -> None:
    requests = [
        _request("r-2", "KX-SAME"),
        _request("r-1", "KX-SAME"),
    ]
    result = _build(
        rfq_requests=requests,
        l1_market_universe=_universe("KX-SAME"),
        l2_market_universe=_universe("KX-SAME"),
    )

    assert [row["request_id"] for row in result["mapping_rows"]] == [
        "r-1", "r-2",
    ]
    assert result["mapping_input_ticker_count"] == 2


def test_case_prefix_and_series_like_values_never_fallback_match() -> None:
    requests = [
        _request("r-case", "kx-both"),
        _request("r-prefix", "KX-BOT"),
        _request("r-series", "KX"),
    ]
    result = _build(
        rfq_requests=requests,
        l1_market_universe=_universe("KX-BOTH"),
        l2_market_universe=_universe("KX-BOTH"),
    )

    assert {row["mapping_state"] for row in result["mapping_rows"]} == {
        "MISSING_L1_L2"
    }
    assert result["dq_count"] == 3


@pytest.mark.parametrize(
    "created_ts",
    [
        "2026-07-16T23:59:59.999999Z",
        "2026-07-18T00:00:00Z",
    ],
)
def test_created_ts_must_belong_to_analysis_date(created_ts: str) -> None:
    _assert_code(
        "CROSS_DATE_CREATED_TS",
        _build,
        rfq_requests=[_request("r", "KX-A", created_ts=created_ts)],
    )


@pytest.mark.parametrize(
    "created_ts",
    [
        "2026-07-17 12:00:00Z",
        "2026-07-17T12:00:00+00:00",
        "2026-07-17T12:00Z",
        "2026-07-17T25:00:00Z",
        1,
        True,
    ],
)
def test_created_ts_must_be_valid_canonical_utc(created_ts) -> None:
    _assert_code(
        "INVALID_CREATED_TS",
        _build,
        rfq_requests=[_request("r", "KX-A", created_ts=created_ts)],
    )


def test_cross_boundary_event_window_retains_rows_and_adds_dq() -> None:
    requests = [
        _request("r-early", "KX-A", created_ts="2026-07-17T00:00:00Z"),
        _request(
            "r-late",
            "KX-B",
            created_ts="2026-07-17T23:59:59.999Z",
        ),
    ]
    result = _build(
        rfq_requests=requests,
        l1_market_universe=_universe("KX-A", "KX-B"),
        l2_market_universe=_universe("KX-A", "KX-B"),
        pre_event_window_ms=1,
        post_event_window_ms=1,
    )

    assert len(result["mapping_rows"]) == 2
    assert all(row["mapping_state"] == "MAPPED_L1_L2" for row in result["mapping_rows"])
    assert all(
        row["event_window_within_base_date"] is False
        for row in result["mapping_rows"]
    )
    assert [row["dq_code"] for row in result["dq_ledger"]] == [
        "EVENT_WINDOW_OUTSIDE_BASE_DATE",
        "EVENT_WINDOW_OUTSIDE_BASE_DATE",
    ]


def test_zero_window_at_day_start_is_inside_base_date() -> None:
    result = _build(
        rfq_requests=[
            _request("r", "KX-A", created_ts="2026-07-17T00:00:00Z")
        ],
        l1_market_universe=_universe("KX-A"),
        l2_market_universe=_universe("KX-A"),
        pre_event_window_ms=0,
        post_event_window_ms=0,
    )
    assert result["mapping_rows"][0]["event_window_within_base_date"] is True
    assert result["dq_ledger"] == []


@pytest.mark.parametrize(
    ("post_window_ms", "within_date", "expected_dq"),
    [
        (999, True, []),
        (1_000, False, ["EVENT_WINDOW_OUTSIDE_BASE_DATE"]),
    ],
)
def test_closed_window_right_boundary(
    post_window_ms: int,
    within_date: bool,
    expected_dq: list[str],
) -> None:
    result = _build(
        rfq_requests=[
            _request(
                "r",
                "KX-A",
                created_ts="2026-07-17T23:59:59Z",
            )
        ],
        l1_market_universe=_universe("KX-A"),
        l2_market_universe=_universe("KX-A"),
        pre_event_window_ms=0,
        post_event_window_ms=post_window_ms,
    )
    assert result["mapping_rows"][0][
        "event_window_within_base_date"
    ] is within_date
    assert [row["dq_code"] for row in result["dq_ledger"]] == expected_dq


def test_missing_market_and_outside_window_emit_both_dq_codes() -> None:
    result = _build(
        rfq_requests=[
            _request("r", "KX-NONE", created_ts="2026-07-17T00:00:00Z")
        ],
        l1_market_universe=[],
        l2_market_universe=[],
        pre_event_window_ms=1,
        post_event_window_ms=0,
    )
    assert [row["dq_code"] for row in result["dq_ledger"]] == [
        "EVENT_WINDOW_OUTSIDE_BASE_DATE",
        "MISSING_L1_L2",
    ]


def test_collection_ticker_without_legs_fails_closed() -> None:
    _assert_code(
        "RFQ_COMBO_INCOMPLETE",
        _build,
        rfq_requests=[
            _request("r", "KX-TOP", collection_ticker="KX-COLLECTION")
        ],
    )


def test_null_collection_with_empty_legs_is_a_single() -> None:
    result = _build(
        rfq_requests=[_request("r", "KX-A")],
        l1_market_universe=_universe("KX-A"),
        l2_market_universe=_universe("KX-A"),
    )
    assert result["mapping_rows"][0]["request_kind"] == "SINGLE"
    assert result["ignored_combo_top_levels"] == []


@pytest.mark.parametrize("bad_window", [-1, True, False, 1.0, "1", None])
def test_pre_window_is_explicit_nonnegative_plain_integer(bad_window) -> None:
    _assert_code("INVALID_EVENT_WINDOW", _build, pre_event_window_ms=bad_window)


@pytest.mark.parametrize("bad_window", [-1, True, False, 1.0, "1", None])
def test_post_window_is_explicit_nonnegative_plain_integer(bad_window) -> None:
    _assert_code("INVALID_EVENT_WINDOW", _build, post_event_window_ms=bad_window)


def test_duplicate_request_id_is_rejected() -> None:
    requests = [_request("same", "KX-A"), _request("same", "KX-B")]
    _assert_code("RFQ_INPUT_DUPLICATE", _build, rfq_requests=requests)


def test_duplicate_combo_leg_is_rejected() -> None:
    requests = [_request("r", "KX-TOP", legs=["KX-A", "KX-A"])]
    _assert_code("RFQ_INPUT_DUPLICATE", _build, rfq_requests=requests)


@pytest.mark.parametrize("universe_name", ["l1_market_universe", "l2_market_universe"])
def test_duplicate_universe_ticker_is_rejected(universe_name: str) -> None:
    _assert_code(
        "UNIVERSE_DUPLICATE",
        _build,
        **{universe_name: _universe("KX-A", "KX-A")},
    )


@pytest.mark.parametrize("universe_name", ["l1_market_universe", "l2_market_universe"])
def test_cross_date_universe_is_rejected(universe_name: str) -> None:
    _assert_code(
        "CROSS_DATE_UNIVERSE",
        _build,
        **{universe_name: _universe("KX-A", date="2026-07-16")},
    )


def test_empty_request_input_is_complete_but_never_research_ready() -> None:
    result = _build(rfq_requests=[])

    assert result["rfq_request_count"] == 0
    assert result["mapping_input_ticker_count"] == 0
    assert result["mapping_rows"] == []
    assert result["mapping_row_set_sha256"] == mapping.canonical_sha256([])
    assert result["ignored_combo_top_levels"] == []
    assert result["dq_ledger"] == []
    assert result["dq_count"] == 0
    assert result["all_mapping_components_retained"] is True
    assert result["research_eligible"] is False
    assert result["research_ready"] is False
    assert _validate(result, rfq_requests=[]) == result


def test_extra_request_field_is_rejected_without_event_fallback() -> None:
    request = _request("r", "KX-A")
    request["event_ticker"] = "KX-EVENT"
    _assert_code("SCHEMA_FIELDS", _build, rfq_requests=[request])


def test_invalid_request_shapes_and_identifiers_are_rejected() -> None:
    bad_legs = _request("r", "KX-A")
    bad_legs["mve_selected_legs"] = None
    _assert_code("RFQ_INPUT_INVALID", _build, rfq_requests=[bad_legs])

    bad_ticker = _request("r", " KX-A")
    _assert_code("INVALID_IDENTIFIER", _build, rfq_requests=[bad_ticker])

    bad_collection = _request("r", "KX-A", legs=["KX-B"])
    bad_collection["mve_collection_ticker"] = ""
    _assert_code("INVALID_IDENTIFIER", _build, rfq_requests=[bad_collection])


def test_invalid_analysis_date_and_sha_bindings_are_rejected() -> None:
    _assert_code("INVALID_DATE", _build, analysis_date="2026-02-30")
    _assert_code("INVALID_SHA256", _build, base_binding_sha256="A" * 64)
    _assert_code(
        "INVALID_SHA256",
        _build,
        analysis_rfq_object_set_sha256="b" * 63,
    )
    _assert_code("INVALID_SHA256", _build, time_contract_sha256=True)


def test_direct_digest_tamper_fails_before_rebuild() -> None:
    result = _build()
    result["state"] = "PUBLISHED"
    _assert_code("MAPPING_DIGEST_MISMATCH", _validate, result)


def test_tamper_and_rehash_cannot_change_bool_to_int() -> None:
    result = _build()
    result["mapping_rows"][0]["l1_present"] = 1
    _reseal(result)
    _assert_code("MAPPING_REBUILD_MISMATCH", _validate, result)


def test_tamper_and_rehash_cannot_change_mapping_or_dq() -> None:
    changed_mapping = _build()
    changed_mapping["mapping_rows"][0]["mapping_state"] = "MAPPED_L1_L2"
    _reseal(changed_mapping)
    _assert_code("MAPPING_REBUILD_MISMATCH", _validate, changed_mapping)

    changed_dq = _build()
    changed_dq["dq_ledger"][0]["dq_code"] = "MISSING_L1_L2"
    _reseal(changed_dq)
    _assert_code("MAPPING_REBUILD_MISMATCH", _validate, changed_dq)


def test_tamper_and_rehash_cannot_change_ignored_combo_audit() -> None:
    result = _build()
    result["ignored_combo_top_levels"][0]["market_ticker"] = "KX-HIDDEN"
    _reseal(result)
    _assert_code("MAPPING_REBUILD_MISMATCH", _validate, result)


def test_tamper_and_rehash_cannot_change_window_or_source_binding() -> None:
    changed_window = _build()
    changed_window["pre_event_window_ms"] = 0
    _reseal(changed_window)
    _assert_code("MAPPING_REBUILD_MISMATCH", _validate, changed_window)

    changed_binding = _build()
    changed_binding["base_binding_sha256"] = "d" * 64
    _reseal(changed_binding)
    _assert_code("MAPPING_REBUILD_MISMATCH", _validate, changed_binding)

    changed_time_contract = _build()
    changed_time_contract["time_contract_sha256"] = "d" * 64
    _reseal(changed_time_contract)
    _assert_code(
        "MAPPING_REBUILD_MISMATCH",
        _validate,
        changed_time_contract,
    )


def test_output_schema_is_exact() -> None:
    extra = _build()
    extra["extra"] = None
    _assert_code("SCHEMA_FIELDS", _validate, extra)

    missing = _build()
    missing.pop("research_ready")
    _assert_code("SCHEMA_FIELDS", _validate, missing)
