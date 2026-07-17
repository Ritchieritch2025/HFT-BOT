#!/usr/bin/env python3
"""Pure deterministic RFQ-to-base-market mapping for the fresh RFQ lane.

The caller supplies normalized RFQ requests and date-bound L1/L2 market
universes.  This module performs no filesystem, network, AWS, resolver, or
publication I/O.  It deliberately makes no exact-GET or research-readiness
claim: the output is a body-free, local, unpublished derivation that must later
be bound to independently resolved inputs.

Normalized RFQ request schema::

    {
      "request_id": "...",
      "created_ts": "2026-07-17T12:34:56Z",
      "market_ticker": "...",
      "mve_collection_ticker": null,
      "mve_selected_legs": [{"market_ticker": "..."}, ...]
    }

An empty leg list means single-market and maps only the top-level ticker.  A
non-empty leg list means combo and maps only the leg tickers; the combo's
top-level ticker is never treated as a missing base market.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
from typing import Any


SCHEMA = "fresh-rfq-market-mapping-v1"
STATE = "LOCALLY_DERIVED_UNPUBLISHED_UNVERIFIED"
MATCHING_POLICY = "EXACT_CASE_SENSITIVE_NO_FALLBACK"
CROSS_DATE_POLICY = "DQ_NO_CROSS_DATE_BORROW"
COMBO_TOP_LEVEL_POLICY = "IGNORE_WHEN_LEGS_NONEMPTY"
EVENT_WINDOW_POLICY = "RETAIN_ROW_DQ_NO_CROSS_DATE_BORROW"
EVENT_WINDOW_INTERVAL_CONTRACT = "CLOSED_WINDOW_WITHIN_HALF_OPEN_BASE_DATE"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")

REQUEST_FIELDS = {
    "request_id", "created_ts", "market_ticker", "mve_collection_ticker",
    "mve_selected_legs",
}
LEG_FIELDS = {"market_ticker"}
UNIVERSE_ROW_FIELDS = {"analysis_date", "market_ticker"}
MAPPING_ROW_FIELDS = {
    "request_id", "created_ts", "request_kind", "component_index",
    "source_field", "market_ticker", "l1_present", "l2_present",
    "mapping_state", "event_window_within_base_date",
}
DQ_ROW_FIELDS = {
    "request_id", "created_ts", "request_kind", "component_index",
    "market_ticker", "dq_code",
}
IGNORED_COMBO_FIELDS = {
    "request_id", "created_ts", "market_ticker", "audit_reason",
}
OUTPUT_FIELDS = {
    "schema", "state", "verification_state", "analysis_date",
    "base_binding_sha256", "analysis_rfq_object_set_sha256",
    "time_contract_sha256",
    "rfq_input_sha256", "rfq_request_count", "mapping_input_ticker_count",
    "l1_market_universe_sha256", "l1_market_universe_count",
    "l2_market_universe_sha256", "l2_market_universe_count",
    "pre_event_window_ms", "post_event_window_ms", "matching_policy",
    "cross_date_policy", "combo_top_level_policy", "event_window_policy",
    "event_window_interval_contract",
    "ignored_combo_top_levels", "ignored_combo_top_level_count",
    "ignored_combo_top_level_sha256", "mapping_rows",
    "mapping_row_set_sha256", "dq_ledger", "dq_ledger_sha256", "dq_count",
    "all_mapping_components_retained", "source_objects_exact_get_verified",
    "data_objects_copied", "aws_write_authorized", "research_eligible",
    "research_ready", "mapping_sha256",
}


class FreshRfqMarketMappingError(ValueError):
    """Stable fail-closed error for the pure market-mapping contract."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _fail(code: str, detail: str) -> None:
    raise FreshRfqMarketMappingError(code, detail)


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        _fail("NON_CANONICAL_VALUE", str(exc))


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _canonical_exact_equal(left: Any, right: Any) -> bool:
    return canonical_bytes(left) == canonical_bytes(right)


def _exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        _fail("SCHEMA_FIELDS", f"{label} fields differ from contract")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        _fail("INVALID_SHA256", f"{label} must be lowercase SHA-256")
    return value


def _date(value: Any, label: str = "analysis_date") -> str:
    if not isinstance(value, str) or DATE_RE.fullmatch(value) is None:
        _fail("INVALID_DATE", f"{label} must be YYYY-MM-DD")
    try:
        dt.date.fromisoformat(value)
    except ValueError as exc:
        _fail("INVALID_DATE", f"{label}: {exc}")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or IDENTIFIER_RE.fullmatch(value) is None:
        _fail(
            "INVALID_IDENTIFIER",
            f"{label} must be exact non-whitespace identifier text",
        )
    return value


def _window_ms(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        _fail("INVALID_EVENT_WINDOW", f"{label} must be a non-negative integer")
    return value


def _created_ts(
    value: Any,
    analysis_date: str,
    label: str,
) -> tuple[str, dt.datetime]:
    if not isinstance(value, str) or UTC_RE.fullmatch(value) is None:
        _fail("INVALID_CREATED_TS", f"{label} must be canonical UTC ending in Z")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        _fail("INVALID_CREATED_TS", f"{label}: {exc}")
    day_start = dt.datetime.combine(
        dt.date.fromisoformat(analysis_date),
        dt.time.min,
        tzinfo=dt.timezone.utc,
    )
    if not day_start <= parsed < day_start + dt.timedelta(days=1):
        _fail(
            "CROSS_DATE_CREATED_TS",
            f"{label} must be inside analysis date D",
        )
    return value, parsed


def _normalize_requests(
    value: Any,
    analysis_date: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        _fail("RFQ_INPUT_INVALID", "rfq_requests must be a non-empty list")
    requests: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(value):
        row = _exact_keys(raw, REQUEST_FIELDS, f"rfq_requests[{index}]")
        request_id = _identifier(row["request_id"], f"rfq_requests[{index}].request_id")
        if request_id in seen_ids:
            _fail("RFQ_INPUT_DUPLICATE", f"duplicate request_id {request_id!r}")
        seen_ids.add(request_id)
        created_ts, _parsed_created_ts = _created_ts(
            row["created_ts"],
            analysis_date,
            f"rfq_requests[{index}].created_ts",
        )
        top_level = _identifier(
            row["market_ticker"], f"rfq_requests[{index}].market_ticker"
        )
        raw_collection_ticker = row["mve_collection_ticker"]
        collection_ticker = None
        if raw_collection_ticker is not None:
            collection_ticker = _identifier(
                raw_collection_ticker,
                f"rfq_requests[{index}].mve_collection_ticker",
            )
        raw_legs = row["mve_selected_legs"]
        if not isinstance(raw_legs, list):
            _fail("RFQ_INPUT_INVALID", "mve_selected_legs must be a list")
        legs: list[dict[str, str]] = []
        seen_leg_tickers: set[str] = set()
        for leg_index, raw_leg in enumerate(raw_legs):
            leg = _exact_keys(
                raw_leg,
                LEG_FIELDS,
                f"rfq_requests[{index}].mve_selected_legs[{leg_index}]",
            )
            ticker = _identifier(
                leg["market_ticker"],
                f"rfq_requests[{index}].mve_selected_legs[{leg_index}].market_ticker",
            )
            if ticker in seen_leg_tickers:
                _fail(
                    "RFQ_INPUT_DUPLICATE",
                    f"request {request_id!r} repeats combo leg {ticker!r}",
                )
            seen_leg_tickers.add(ticker)
            legs.append({"market_ticker": ticker})
        if collection_ticker is not None and not legs:
            _fail(
                "RFQ_COMBO_INCOMPLETE",
                f"request {request_id!r} has collection ticker without legs",
            )
        legs.sort(key=lambda leg: leg["market_ticker"])
        requests.append({
            "request_id": request_id,
            "created_ts": created_ts,
            "market_ticker": top_level,
            "mve_collection_ticker": collection_ticker,
            "mve_selected_legs": legs,
        })
    requests.sort(key=lambda row: row["request_id"])
    return requests


def _normalize_universe(
    value: Any, analysis_date: str, label: str,
) -> list[dict[str, str]]:
    if not isinstance(value, list):
        _fail("UNIVERSE_INVALID", f"{label} must be a list")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        row = _exact_keys(raw, UNIVERSE_ROW_FIELDS, f"{label}[{index}]")
        row_date = _date(row["analysis_date"], f"{label}[{index}].analysis_date")
        if row_date != analysis_date:
            _fail(
                "CROSS_DATE_UNIVERSE",
                f"{label}[{index}] does not belong to analysis date D",
            )
        ticker = _identifier(row["market_ticker"], f"{label}[{index}].market_ticker")
        if ticker in seen:
            _fail("UNIVERSE_DUPLICATE", f"{label} repeats ticker {ticker!r}")
        seen.add(ticker)
        rows.append({"analysis_date": row_date, "market_ticker": ticker})
    rows.sort(key=lambda row: row["market_ticker"])
    return rows


def _mapping_state(l1_present: bool, l2_present: bool) -> str:
    if l1_present and l2_present:
        return "MAPPED_L1_L2"
    if not l1_present and l2_present:
        return "MISSING_L1"
    if l1_present and not l2_present:
        return "MISSING_L2"
    return "MISSING_L1_L2"


def _event_window_within_base_date(
    created_ts: str,
    analysis_date: str,
    pre_window_ms: int,
    post_window_ms: int,
) -> bool:
    _text, event_time = _created_ts(created_ts, analysis_date, "created_ts")
    day_start = dt.datetime.combine(
        dt.date.fromisoformat(analysis_date),
        dt.time.min,
        tzinfo=dt.timezone.utc,
    )
    day_end = day_start + dt.timedelta(days=1)
    elapsed = event_time - day_start
    remaining = day_end - event_time
    elapsed_us = (
        (elapsed.days * 86_400 + elapsed.seconds) * 1_000_000
        + elapsed.microseconds
    )
    remaining_us = (
        (remaining.days * 86_400 + remaining.seconds) * 1_000_000
        + remaining.microseconds
    )
    return (
        pre_window_ms * 1_000 <= elapsed_us
        and post_window_ms * 1_000 < remaining_us
    )


def _derive_market_mapping(
    *,
    analysis_date: Any,
    base_binding_sha256: Any,
    analysis_rfq_object_set_sha256: Any,
    time_contract_sha256: Any,
    rfq_requests: Any,
    l1_market_universe: Any,
    l2_market_universe: Any,
    pre_event_window_ms: Any,
    post_event_window_ms: Any,
) -> dict[str, Any]:
    date_text = _date(analysis_date)
    base_sha = _sha256(base_binding_sha256, "base_binding_sha256")
    rfq_object_set_sha = _sha256(
        analysis_rfq_object_set_sha256,
        "analysis_rfq_object_set_sha256",
    )
    time_contract_sha = _sha256(time_contract_sha256, "time_contract_sha256")
    pre_window = _window_ms(pre_event_window_ms, "pre_event_window_ms")
    post_window = _window_ms(post_event_window_ms, "post_event_window_ms")
    requests = _normalize_requests(rfq_requests, date_text)
    l1_rows = _normalize_universe(l1_market_universe, date_text, "l1_market_universe")
    l2_rows = _normalize_universe(l2_market_universe, date_text, "l2_market_universe")
    l1_tickers = {row["market_ticker"] for row in l1_rows}
    l2_tickers = {row["market_ticker"] for row in l2_rows}

    mapping_rows: list[dict[str, Any]] = []
    dq_ledger: list[dict[str, Any]] = []
    ignored_combo_top_levels: list[dict[str, str]] = []
    for request in requests:
        legs = request["mve_selected_legs"]
        request_kind = "COMBO" if legs else "SINGLE"
        if legs:
            ignored_combo_top_levels.append({
                "request_id": request["request_id"],
                "created_ts": request["created_ts"],
                "market_ticker": request["market_ticker"],
                "audit_reason": "COMBO_TOP_LEVEL_NOT_A_BASE_MARKET_COMPONENT",
            })
        window_within_date = _event_window_within_base_date(
            request["created_ts"],
            date_text,
            pre_window,
            post_window,
        )
        components = [row["market_ticker"] for row in legs] if legs else [
            request["market_ticker"]
        ]
        source_field = "mve_selected_legs.market_ticker" if legs else "market_ticker"
        for component_index, ticker in enumerate(components):
            l1_present = ticker in l1_tickers
            l2_present = ticker in l2_tickers
            state = _mapping_state(l1_present, l2_present)
            mapping_rows.append({
                "request_id": request["request_id"],
                "created_ts": request["created_ts"],
                "request_kind": request_kind,
                "component_index": component_index,
                "source_field": source_field,
                "market_ticker": ticker,
                "l1_present": l1_present,
                "l2_present": l2_present,
                "mapping_state": state,
                "event_window_within_base_date": window_within_date,
            })
            if state != "MAPPED_L1_L2":
                dq_ledger.append({
                    "request_id": request["request_id"],
                    "created_ts": request["created_ts"],
                    "request_kind": request_kind,
                    "component_index": component_index,
                    "market_ticker": ticker,
                    "dq_code": state,
                })
            if not window_within_date:
                dq_ledger.append({
                    "request_id": request["request_id"],
                    "created_ts": request["created_ts"],
                    "request_kind": request_kind,
                    "component_index": component_index,
                    "market_ticker": ticker,
                    "dq_code": "EVENT_WINDOW_OUTSIDE_BASE_DATE",
                })

    for index, row in enumerate(mapping_rows):
        _exact_keys(row, MAPPING_ROW_FIELDS, f"mapping_rows[{index}]")
    for index, row in enumerate(ignored_combo_top_levels):
        _exact_keys(
            row,
            IGNORED_COMBO_FIELDS,
            f"ignored_combo_top_levels[{index}]",
        )
    dq_ledger.sort(key=lambda row: (
        row["request_id"], row["component_index"], row["dq_code"],
    ))
    for index, row in enumerate(dq_ledger):
        _exact_keys(row, DQ_ROW_FIELDS, f"dq_ledger[{index}]")

    result = {
        "schema": SCHEMA,
        "state": STATE,
        "verification_state": "INPUT_CONTENT_NOT_EXACT_GET_VERIFIED",
        "analysis_date": date_text,
        "base_binding_sha256": base_sha,
        "analysis_rfq_object_set_sha256": rfq_object_set_sha,
        "time_contract_sha256": time_contract_sha,
        "rfq_input_sha256": canonical_sha256(requests),
        "rfq_request_count": len(requests),
        "mapping_input_ticker_count": len(mapping_rows),
        "l1_market_universe_sha256": canonical_sha256(l1_rows),
        "l1_market_universe_count": len(l1_rows),
        "l2_market_universe_sha256": canonical_sha256(l2_rows),
        "l2_market_universe_count": len(l2_rows),
        "pre_event_window_ms": pre_window,
        "post_event_window_ms": post_window,
        "matching_policy": MATCHING_POLICY,
        "cross_date_policy": CROSS_DATE_POLICY,
        "combo_top_level_policy": COMBO_TOP_LEVEL_POLICY,
        "event_window_policy": EVENT_WINDOW_POLICY,
        "event_window_interval_contract": EVENT_WINDOW_INTERVAL_CONTRACT,
        "ignored_combo_top_levels": ignored_combo_top_levels,
        "ignored_combo_top_level_count": len(ignored_combo_top_levels),
        "ignored_combo_top_level_sha256": canonical_sha256(
            ignored_combo_top_levels
        ),
        "mapping_rows": mapping_rows,
        "mapping_row_set_sha256": canonical_sha256(mapping_rows),
        "dq_ledger": dq_ledger,
        "dq_ledger_sha256": canonical_sha256(dq_ledger),
        "dq_count": len(dq_ledger),
        "all_mapping_components_retained": True,
        "source_objects_exact_get_verified": False,
        "data_objects_copied": 0,
        "aws_write_authorized": False,
        "research_eligible": False,
        "research_ready": False,
    }
    result["mapping_sha256"] = canonical_sha256(result)
    return result


def build_market_mapping(
    *,
    analysis_date: str,
    base_binding_sha256: str,
    analysis_rfq_object_set_sha256: str,
    time_contract_sha256: str,
    rfq_requests: list[dict[str, Any]],
    l1_market_universe: list[dict[str, Any]],
    l2_market_universe: list[dict[str, Any]],
    pre_event_window_ms: int,
    post_event_window_ms: int,
) -> dict[str, Any]:
    """Build one deterministic, body-free local mapping document."""
    result = _derive_market_mapping(
        analysis_date=analysis_date,
        base_binding_sha256=base_binding_sha256,
        analysis_rfq_object_set_sha256=analysis_rfq_object_set_sha256,
        time_contract_sha256=time_contract_sha256,
        rfq_requests=rfq_requests,
        l1_market_universe=l1_market_universe,
        l2_market_universe=l2_market_universe,
        pre_event_window_ms=pre_event_window_ms,
        post_event_window_ms=post_event_window_ms,
    )
    return validate_market_mapping(
        result,
        analysis_date=analysis_date,
        base_binding_sha256=base_binding_sha256,
        analysis_rfq_object_set_sha256=analysis_rfq_object_set_sha256,
        time_contract_sha256=time_contract_sha256,
        rfq_requests=rfq_requests,
        l1_market_universe=l1_market_universe,
        l2_market_universe=l2_market_universe,
        pre_event_window_ms=pre_event_window_ms,
        post_event_window_ms=post_event_window_ms,
    )


def validate_market_mapping(
    value: Any,
    *,
    analysis_date: str,
    base_binding_sha256: str,
    analysis_rfq_object_set_sha256: str,
    time_contract_sha256: str,
    rfq_requests: list[dict[str, Any]],
    l1_market_universe: list[dict[str, Any]],
    l2_market_universe: list[dict[str, Any]],
    pre_event_window_ms: int,
    post_event_window_ms: int,
) -> dict[str, Any]:
    """Strictly rebuild and validate a mapping from the supplied pure inputs."""
    value = _exact_keys(value, OUTPUT_FIELDS, "market mapping")
    supplied_sha = _sha256(value["mapping_sha256"], "mapping_sha256")
    unsigned = copy.deepcopy(value)
    unsigned.pop("mapping_sha256")
    if supplied_sha != canonical_sha256(unsigned):
        _fail("MAPPING_DIGEST_MISMATCH", "mapping_sha256 mismatch")

    expected = _derive_market_mapping(
        analysis_date=analysis_date,
        base_binding_sha256=base_binding_sha256,
        analysis_rfq_object_set_sha256=analysis_rfq_object_set_sha256,
        time_contract_sha256=time_contract_sha256,
        rfq_requests=rfq_requests,
        l1_market_universe=l1_market_universe,
        l2_market_universe=l2_market_universe,
        pre_event_window_ms=pre_event_window_ms,
        post_event_window_ms=post_event_window_ms,
    )
    if not _canonical_exact_equal(value, expected):
        _fail("MAPPING_REBUILD_MISMATCH", "mapping differs from exact pure rebuild")
    return copy.deepcopy(value)
