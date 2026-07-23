"""Tests for read-only, redacted private fee capture."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.research.pnl_spine.capture_private_fee_truth import (  # noqa: E402
    METHOD,
    PrivateFeeCaptureError,
    canonical_json_bytes,
    redact_fill,
    sha256_bytes,
)


def raw_fill() -> dict[str, object]:
    return {
        "fill_id": "private-fill-id",
        "trade_id": "private-trade-id",
        "order_id": "private-order-id",
        "ticker": "KXMLBGAME-26JUL231310MINCLE-CLE",
        "market_ticker": "KXMLBGAME-26JUL231310MINCLE-CLE",
        "count_fp": "32.09",
        "yes_price_dollars": "0.9300",
        "no_price_dollars": "0.0700",
        "is_taker": True,
        "fee_cost": "0.146500",
        "created_time": "2026-07-17T00:16:29.970513Z",
        "subaccount_number": 0,
        "ts": 1,
    }


def test_capture_code_has_a_single_read_only_http_method():
    assert METHOD == "GET"


def test_redaction_retains_fee_truth_but_removes_raw_identifiers_and_ticker():
    raw = raw_fill()
    result = redact_fill(raw)
    assert result["series_ticker"] == "KXMLBGAME"
    assert result["quantity_e2"] == 3_209
    assert result["yes_price_e4"] == 9_300
    assert result["fee_cost_e6"] == 146_500
    rendered = canonical_json_bytes(result)
    for secret_value in (
        raw["fill_id"],
        raw["trade_id"],
        raw["order_id"],
        raw["market_ticker"],
    ):
        assert str(secret_value).encode() not in rendered
    assert result["fill_ref"] == sha256_bytes(b"private-fill-id")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("count_fp", "1.001"),
        ("yes_price_dollars", "0.12345"),
        ("fee_cost", "0.0000001"),
        ("is_taker", 1),
        ("created_time", "2026-07-17T00:00:00-04:00"),
    ],
)
def test_precision_and_schema_drift_fail_closed(field: str, value: object):
    row = raw_fill()
    row[field] = value
    with pytest.raises(PrivateFeeCaptureError):
        redact_fill(row)


def test_missing_new_api_field_fails_closed():
    row = raw_fill()
    del row["fee_cost"]
    with pytest.raises(PrivateFeeCaptureError, match="missing"):
        redact_fill(row)
