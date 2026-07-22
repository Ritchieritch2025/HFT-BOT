#!/usr/bin/env python3
"""Smoke and claim-safety tests for the C1 report publisher."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "research"))

import c1_real_fill_report as report  # noqa: E402


def fixture() -> dict:
    dates = ["2026-07-12", "2026-07-15", "2026-07-17"]
    tracks = list(report.TRACK_ORDER)
    fill_rates = []
    for timer in (200_000, 300_000):
        for date_i, date in enumerate(dates):
            for track_i, track in enumerate(tracks):
                den = 1000 + date_i * 10
                num = 10 + track_i * 15 + date_i
                fill_rates.append({
                    "date": date, "latency_id": "PRIMARY",
                    "cancel_timer_us": timer, "track": track,
                    "campaigns_accepted": den, "campaigns_eligible": den,
                    "filled_orders": num, "fill_slices": num,
                    "filled_count_e4": num * 10_000,
                    "offered_count_e4": den * 10_000,
                    "fill_rate_order_num": num, "fill_rate_order_den": den,
                    "fill_rate_qty_num_e4": num * 10_000,
                    "fill_rate_qty_den_e4": den * 10_000,
                })
    markouts = []
    attribution = []
    for date_i, date in enumerate(dates):
        for horizon in (100_000, 200_000, 500_000, 1_000_000):
            filled = 100_000
            weighted = (-20 + date_i * 3 + horizon // 100_000) * filled
            markouts.append({
                "date": date, "latency_id": "PRIMARY",
                "cancel_timer_us": 200_000, "track": "STRICT_THROUGH",
                "horizon_us": horizon, "observed_slices": 10,
                "censored_slices": 1, "filled_count_e4": filled,
                "weighted_gross_sum_e8": weighted,
                "weighted_mid_sum_e8_or_null": None,
                "mean_gross_e4": weighted / filled,
                "mean_mid_e4_or_null": None,
            })
            if horizon in (200_000, 1_000_000):
                for group_i, group in enumerate((
                    "REFILL_BY_TIMER", "NON_REFILL_OR_CENSORED"
                )):
                    attribution.append({
                        "date": date, "latency_id": "PRIMARY",
                        "cancel_timer_us": 200_000,
                        "track": "STRICT_THROUGH", "horizon_us": horizon,
                        "refill_group": group, "observed_slices": 5,
                        "censored_slices": 0, "filled_count_e4": 50_000,
                        "weighted_gross_sum_e8": (10 - group_i * 30) * 50_000,
                    })
    return {
        "schema_version": report.SCHEMA_VERSION,
        "experiment_id": "C1-REAL-FILL-TEST",
        "claim_tier": "EXPLORATORY_NON_GATE",
        "fee_state": "NOT_ESTIMABLE_FAIL_CLOSED",
        "verdict": {
            "code": "INDETERMINATE_MORE_CLEAN_DAYS", "precedence": 4,
            "reason": "Synthetic smoke fixture.", "positive_promotion": False,
        },
        "summary": {"generated_at_utc": "2026-07-22T00:00:00Z"},
        "fill_rates": fill_rates,
        "markouts": markouts,
        "attribution": attribution,
        "concentration": [
            {"date": date, "events": 20, "markets": 30,
             "max_market_share": 0.1, "hhi": 0.04}
            for date in dates
        ],
        "exclusions": [
            {"reason": "OVERLAP_SUPPRESSED", "count": 500},
            {"reason": "ACTIVATION_STALE", "count": 100},
        ],
        "queue_invariants": {"state": "PASS"},
        "clock_qc": {"state": "PASS"},
        "provenance": {"source_binding": "abc", "runtime_commit": "def"},
    }


def test_publish_creates_five_charts_sources_self_contained_html_and_pdf(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "C1_AGGREGATES.json").write_text(
        json.dumps(fixture(), sort_keys=True), encoding="utf-8"
    )
    output = tmp_path / "report"
    pdf = tmp_path / "output" / "pdf" / "C1.pdf"
    result = report.publish(input_dir, output, pdf)
    assert len(result["charts"]) == 5
    assert len(result["chart_sources"]) == 5
    assert pdf.stat().st_size > 10_000
    document = (output / "index.html").read_text(encoding="utf-8")
    assert document.count("data:image/png;base64,") == 5
    assert "EXPLORATORY_NON_GATE" in document
    assert "gross" in document
    assert "positive_promotion: false" in document
    assert "net profit" not in document.lower()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("claim_tier", "CONFIRMED"),
        ("fee_state", "ESTIMATED"),
    ],
)
def test_claim_upgrade_and_fee_guess_fail_closed(field, value):
    data = fixture()
    data[field] = value
    with pytest.raises(report.C1ReportError):
        report.validate_aggregates(data)


def test_positive_promotion_fails_closed():
    data = fixture()
    data["verdict"]["positive_promotion"] = True
    with pytest.raises(report.C1ReportError):
        report.validate_aggregates(data)


def test_invalid_ratio_fails_closed():
    data = fixture()
    data["fill_rates"][0]["fill_rate_order_num"] = 1001
    data["fill_rates"][0]["fill_rate_order_den"] = 1000
    with pytest.raises(report.C1ReportError):
        report.validate_aggregates(data)
