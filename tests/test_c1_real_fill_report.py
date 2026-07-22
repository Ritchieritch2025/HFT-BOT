#!/usr/bin/env python3
"""Adversarial claim-safety and artifact-binding tests for the C1 publisher."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "research"))

import c1_real_fill_report as report  # noqa: E402


def fixture() -> dict:
    fill_rates = []
    for date in report.ELIGIBLE_DATES:
        for latency in report.LATENCY_ORDER:
            for timer in report.CANCEL_TIMERS:
                for track_i, track in enumerate(report.TRACK_ORDER):
                    eligible = 100
                    filled = 5 + track_i * 5
                    cohort = 80
                    cohort_filled = 4 + track_i * 4
                    fill_rates.append({
                        "date": date,
                        "latency_id": latency,
                        "cancel_timer_us": timer,
                        "track": track,
                        "campaigns_eligible": eligible,
                        "filled_orders": filled,
                        "fill_slices": filled,
                        "filled_count_e4": filled * 10_000,
                        "campaigns_accepted": 100,
                        "offered_count_e4": eligible * 10_000,
                        "fill_rate_order_num": filled,
                        "fill_rate_order_den": eligible,
                        "fill_rate_qty_num_e4": filled * 10_000,
                        "fill_rate_qty_den_e4": eligible * 10_000,
                        "fill_rate_order": filled / eligible,
                        "fill_rate_quantity": filled / eligible,
                        "denominator_scope": (
                            "QUEUE_RECONSTRUCTABLE_ACTIVATIONS"
                            if track == "QUEUE_PESSIMISTIC"
                            else "ALL_ACTIVATION_ELIGIBLE"
                        ),
                        "queue_band_cohort_id": "QUEUE_RECONSTRUCTABLE_ACTIVATIONS",
                        "queue_band_fill_rate_order_num": cohort_filled,
                        "queue_band_fill_rate_order_den": cohort,
                        "queue_band_fill_rate_qty_num_e4": cohort_filled * 10_000,
                        "queue_band_fill_rate_qty_den_e4": cohort * 10_000,
                    })

    markouts = []
    for date in report.ELIGIBLE_DATES:
        for latency in report.LATENCY_ORDER:
            for timer in report.CANCEL_TIMERS:
                for track_i, track in enumerate(report.TRACK_ORDER):
                    for horizon in report.MARKOUT_HORIZONS:
                        fill_slices = 5 + track_i * 5
                        total = fill_slices * 10_000
                        observed = total // 2
                        censored = total - observed
                        point = -10 * observed
                        mid_twice = -20 * observed
                        entry = 5_000 * censored
                        lower = point - entry
                        upper = point + 10_000 * censored - entry
                        markouts.append({
                            "date": date,
                            "latency_id": latency,
                            "cancel_timer_us": timer,
                            "track": track,
                            "horizon_us": horizon,
                            "total_slices": fill_slices,
                            "observed_slices": fill_slices,
                            "censored_slices": fill_slices,
                            "total_filled_count_e4": total,
                            "observed_count_e4": observed,
                            "censored_count_e4": censored,
                            "weighted_gross_sum_e8": point,
                            "weighted_mid_twice_sum_e8": mid_twice,
                            "weighted_censored_entry_price_sum_e8": entry,
                            "weighted_gross_lower_sum_e8": lower,
                            "weighted_gross_upper_sum_e8": upper,
                            "coverage_count_num_e4": observed,
                            "coverage_count_den_e4": total,
                            "coverage_fraction_diagnostic": observed / total,
                            "mean_gross_e4": point / observed,
                            "mean_mid_e4": mid_twice / (2 * observed),
                        })

    attribution = []
    for markout in markouts:
        remaining_slices = int(markout["total_slices"])
        remaining_total = int(markout["total_filled_count_e4"])
        for group_i, group in enumerate(
            ("REFILL_BY_TIMER", "NON_REFILL_OR_CENSORED")
        ):
            total_slices = (
                remaining_slices // 2 if group_i == 0 else remaining_slices
            )
            total = remaining_total // 2 if group_i == 0 else remaining_total
            remaining_slices -= total_slices
            remaining_total -= total
            observed = total // 2
            censored = total - observed
            point = -10 * observed
            entry = 5_000 * censored
            attribution.append({
                "date": markout["date"],
                "latency_id": markout["latency_id"],
                "cancel_timer_us": markout["cancel_timer_us"],
                "track": markout["track"],
                "refill_group": group,
                "horizon_us": markout["horizon_us"],
                "total_slices": total_slices,
                "observed_slices": total_slices,
                "censored_slices": total_slices,
                "total_filled_count_e4": total,
                "observed_count_e4": observed,
                "censored_count_e4": censored,
                "weighted_gross_sum_e8": point,
                "weighted_censored_entry_price_sum_e8": entry,
                "weighted_gross_lower_sum_e8": point - entry,
                "weighted_gross_upper_sum_e8": (
                    point + 10_000 * censored - entry
                ),
                "coverage_count_num_e4": observed,
                "coverage_count_den_e4": total,
                "mean_gross_e4": point / observed,
            })

    data = {
        "schema_version": report.SCHEMA_VERSION,
        "experiment_id": "C1-REAL-FILL-TEST",
        "claim_tier": "EXPLORATORY_NON_GATE",
        "fee_state": "NOT_ESTIMABLE_FAIL_CLOSED",
        "summary": {
            "eligible_dates": list(report.ELIGIBLE_DATES),
            "work_partitions": 48,
            "headline_track": "STRICT_THROUGH",
            "headline_latency": "PRIMARY",
            "gross_markout_only": True,
            "markout_quantity_policy": "PARTIAL_SAME_OUTCOME_TOP_DEPTH",
            "censored_price_bound_e4": [0, 10_000],
            "net_pnl_estimated": False,
            "live_order_writes": 0,
        },
        "fill_rates": fill_rates,
        "markouts": markouts,
        "attribution": attribution,
        "concentration": [
            {"date": date, "events": 20, "markets": 30,
             "max_market_share": 0.1, "hhi": 0.04}
            for date in report.ELIGIBLE_DATES
        ],
        "concentration_detail": [],
        "exclusions": [
            {"date": "2026-07-12", "reason": "OVERLAP_SUPPRESSED", "count": 500},
            {"date": "2026-07-15", "reason": "OVERLAP_SUPPRESSED", "count": 100},
        ],
        "support": [],
        "queue_invariants": {
            "duplicate_trade_allocation_count": 0,
            "public_volume_exceeded_count": 0,
            "allocation_scope": "UNIQUE_PER_LATENCY_CANCEL_TIMER_FILL_TRACK",
            "negative_l2_delta_creates_fill": False,
            "strict_at_touch_creates_fill": False,
            "global_merged_output_invariants": {
                "duplicate_fill_slice_ids": 0,
                "cross_partition_trade_allocation_failures": 0,
                "cross_partition_public_volume_failures": 0,
                "markout_fill_linkage_failures": 0,
                "markout_identity_failures": 0,
                "markout_quantity_conservation_failures": 0,
            },
            "queue_band_comparison_cohort": "QUEUE_RECONSTRUCTABLE_ACTIVATIONS",
        },
        "clock_qc": {
            "decision_clock": "recv_wall_ns",
            "trade_resolution": "microsecond_ambiguity_against_strategy",
            "past_only_asof": True,
            "state_ttl_us": 250_000,
        },
        "provenance": {"source_binding": "abc", "runtime_commit": "def"},
    }
    data["verdict"] = report._pilot_verdict(
        data["fill_rates"], data["markouts"], report.ELIGIBLE_DATES
    )
    return data


def _write_input(input_dir: Path, data: dict) -> None:
    input_dir.mkdir()
    aggregate = json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n"
    (input_dir / "C1_AGGREGATES.json").write_text(aggregate, encoding="ascii")
    required = {
        "INPUT_RECEIPT.json", "SPEC_RECEIPT.json", "CLOCK_RECEIPT.json",
        "QUEUE_RECEIPT.json", "FEE_RECEIPT.json", "EXCLUSION_WATERFALL.json",
        "RESOURCE_RECEIPT.json", "REPRODUCTION_RECEIPT.json",
        "TABLES/CAMPAIGNS.parquet", "TABLES/FILL_SLICES.parquet",
        "TABLES/MARKOUTS.parquet", "TABLES/FILL_RATES.csv",
        "TABLES/MARKOUTS.csv", "TABLES/ATTRIBUTION.csv",
        "TABLES/CONCENTRATION.csv", "TABLES/SUPPORT.csv",
        "TABLES/EXCLUSIONS.csv",
    }
    for date in report.ELIGIBLE_DATES:
        for bucket in range(16):
            prefix = f"PARTITIONS/date={date}_bucket={bucket:02d}"
            required.update({
                f"{prefix}/CAMPAIGNS.parquet",
                f"{prefix}/FILL_SLICES.parquet",
                f"{prefix}/MARKOUTS.parquet",
                f"{prefix}/EXCLUSIONS.csv",
                f"{prefix}/PARTITION_RECEIPT.json",
            })
    for logical in sorted(required):
        path = input_dir / logical
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((logical + "\n").encode("ascii"))
    artifacts = []
    for path in sorted(input_dir.rglob("*")):
        if not path.is_file():
            continue
        artifacts.append({
            "path": path.relative_to(input_dir).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        })
    analysis_ledger = {
        "schema_version": "c1-analysis-artifact-ledger-v1",
        "artifacts": artifacts,
    }
    ledger_payload = (
        json.dumps(analysis_ledger, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("ascii")
    (input_dir / "ANALYSIS_ARTIFACT_SHA256.json").write_bytes(ledger_payload)
    complete = {
        "schema_version": "c1-real-fill-run-complete-v1",
        "state": "COMPLETE",
        "experiment_id": data["experiment_id"],
        "claim_tier": "EXPLORATORY_NON_GATE",
        "verdict": data["verdict"],
        "partition_receipts": 48,
        "analysis_artifact_ledger_sha256": hashlib.sha256(ledger_payload).hexdigest(),
        "live_order_writes": 0,
        "report_publication_pending": True,
    }
    (input_dir / "C1_RUN_COMPLETE.json").write_text(
        json.dumps(complete, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="ascii",
    )


def test_publish_creates_bound_report_and_publication_ledger(tmp_path: Path) -> None:
    data = fixture()
    input_dir = tmp_path / "input"
    _write_input(input_dir, data)
    output = tmp_path / "report"
    pdf = tmp_path / "output" / "pdf" / "C1.pdf"
    result = report.publish(input_dir, output, pdf)
    assert len(result["charts"]) == 5
    assert len(result["chart_sources"]) == 5
    assert pdf.stat().st_size > 10_000
    document = (output / "index.html").read_text(encoding="utf-8")
    assert document.count("data:image/png;base64,") == 5
    assert "EXPLORATORY_NON_GATE" in document
    assert "合法价格上下界" in document
    assert "positive_promotion: false" in document
    assert "net profit" not in document.lower()
    ledger_path = Path(result["artifact_ledger"])
    ledger = json.loads(ledger_path.read_text())
    assert len(ledger["artifacts"]) == 15
    for row in ledger["artifacts"]:
        assert len(row["sha256"]) == 64 and row["bytes"] > 0
    publication = json.loads(Path(result["publication_complete"]).read_text())
    assert publication["report_artifact_ledger_sha256"] == hashlib.sha256(
        ledger_path.read_bytes()
    ).hexdigest()
    assert publication["positive_promotion"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [("claim_tier", "CONFIRMED"), ("fee_state", "ESTIMATED")],
)
def test_claim_upgrade_and_fee_guess_fail_closed(field: str, value: object) -> None:
    data = fixture()
    data[field] = value
    with pytest.raises(report.C1ReportError):
        report.validate_aggregates(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [("code", "AUTHORIZE_LIVE_TRADING"), ("fees_estimated", True),
     ("live_ready", True), ("positive_promotion", True)],
)
def test_forged_verdict_fields_fail_closed(field: str, value: object) -> None:
    data = fixture()
    data["verdict"][field] = value
    with pytest.raises(report.C1ReportError):
        report.validate_aggregates(data)


def test_missing_duplicate_and_extra_result_cells_fail_closed() -> None:
    for collection in ("fill_rates", "markouts"):
        data = fixture()
        data[collection].pop()
        with pytest.raises(report.C1ReportError):
            report.validate_aggregates(data)
        data = fixture()
        data[collection].append(copy.deepcopy(data[collection][0]))
        with pytest.raises(report.C1ReportError):
            report.validate_aggregates(data)


def test_markout_quantity_bound_and_abi_drift_fail_closed() -> None:
    mutations = []
    data = fixture()
    data["markouts"][0]["censored_count_e4"] += 1
    mutations.append(data)
    data = fixture()
    data["markouts"][0]["weighted_gross_lower_sum_e8"] = 1
    mutations.append(data)
    data = fixture()
    data["markouts"][0]["weighted_mid_sum_e8_or_null"] = None
    mutations.append(data)
    for candidate in mutations:
        with pytest.raises(report.C1ReportError):
            report.validate_aggregates(candidate)


def test_zero_observed_cannot_forge_a_positive_lower_bound() -> None:
    data = fixture()
    row = data["markouts"][0]
    total = int(row["total_filled_count_e4"])
    row.update({
        "observed_slices": 0,
        "censored_slices": row["total_slices"],
        "observed_count_e4": 0,
        "censored_count_e4": total,
        "weighted_gross_sum_e8": 1,
        "weighted_mid_twice_sum_e8": 0,
        "weighted_censored_entry_price_sum_e8": 0,
        "weighted_gross_lower_sum_e8": 1,
        "weighted_gross_upper_sum_e8": 2,
        "coverage_count_num_e4": 0,
        "coverage_count_den_e4": total,
        "coverage_fraction_diagnostic": 0.0,
        "mean_gross_e4": None,
        "mean_mid_e4": None,
    })
    data["verdict"] = report._pilot_verdict(
        data["fill_rates"], data["markouts"], report.ELIGIBLE_DATES
    )
    with pytest.raises(report.C1ReportError, match="mechanically exact"):
        report.validate_aggregates(data)


def test_fill_markout_and_attribution_ledgers_must_cross_conserve() -> None:
    data = fixture()
    data["fill_rates"][0]["filled_count_e4"] += 1
    data["fill_rates"][0]["fill_rate_qty_num_e4"] += 1
    data["fill_rates"][0]["fill_rate_quantity"] = (
        data["fill_rates"][0]["fill_rate_qty_num_e4"]
        / data["fill_rates"][0]["fill_rate_qty_den_e4"]
    )
    with pytest.raises(report.C1ReportError, match="markout grid"):
        report.validate_aggregates(data)

    data = fixture()
    data["attribution"][0]["weighted_censored_entry_price_sum_e8"] += 1
    data["attribution"][0]["weighted_gross_lower_sum_e8"] -= 1
    data["attribution"][0]["weighted_gross_upper_sum_e8"] -= 1
    with pytest.raises(report.C1ReportError, match="sum back"):
        report.validate_aggregates(data)


def test_verdict_is_recomputed_not_trusted() -> None:
    data = fixture()
    data["verdict"]["reason"] = "Looks profitable, trust me."
    with pytest.raises(report.C1ReportError, match="recomputation"):
        report.validate_aggregates(data)


def test_invalid_fill_and_queue_ratios_fail_closed() -> None:
    data = fixture()
    data["fill_rates"][0]["fill_rate_order_num"] = 101
    with pytest.raises(report.C1ReportError):
        report.validate_aggregates(data)
    data = fixture()
    data["fill_rates"][0]["queue_band_fill_rate_qty_num_e4"] += 1_000_000
    with pytest.raises(report.C1ReportError):
        report.validate_aggregates(data)


def test_publication_refuses_unbound_or_preexisting_destinations(tmp_path: Path) -> None:
    data = fixture()
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "C1_AGGREGATES.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(report.C1ReportError, match="completion"):
        report.publish(input_dir, tmp_path / "report", tmp_path / "pdf" / "x.pdf")

    input_dir = tmp_path / "bound"
    _write_input(input_dir, data)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(report.C1ReportError, match="must not already exist"):
        report.publish(input_dir, existing, tmp_path / "pdf2" / "x.pdf")


def test_publication_verifies_every_analysis_artifact_byte(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    _write_input(input_dir, fixture())
    with (input_dir / "TABLES" / "MARKOUTS.parquet").open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(report.C1ReportError, match="byte mismatch"):
        report.publish(input_dir, tmp_path / "report", tmp_path / "pdf" / "x.pdf")


def test_coordinated_ledger_omission_still_fails_required_set(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    _write_input(input_dir, fixture())
    ledger_path = input_dir / "ANALYSIS_ARTIFACT_SHA256.json"
    ledger = json.loads(ledger_path.read_text(encoding="ascii"))
    ledger["artifacts"] = [
        row for row in ledger["artifacts"]
        if row["path"] != "PARTITIONS/date=2026-07-12_bucket=00/MARKOUTS.parquet"
    ]
    payload = (
        json.dumps(ledger, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("ascii")
    ledger_path.write_bytes(payload)
    complete_path = input_dir / "C1_RUN_COMPLETE.json"
    complete = json.loads(complete_path.read_text(encoding="ascii"))
    complete["analysis_artifact_ledger_sha256"] = hashlib.sha256(payload).hexdigest()
    complete_path.write_text(
        json.dumps(complete, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="ascii",
    )
    with pytest.raises(report.C1ReportError, match="omits required"):
        report.publish(input_dir, tmp_path / "report", tmp_path / "pdf" / "x.pdf")
