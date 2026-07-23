"""Adversarial tests for hash-pinned C1/Deep03 real-data adaptation."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.research.pnl_spine.real_data import (  # noqa: E402
    RealDataError,
    RealInputPaths,
    RealInputPins,
    audit_real_data,
    load_a11_normalized_rows,
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_parquet(path: Path, select_sql: str) -> None:
    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect(database=":memory:")
    try:
        connection.execute(
            f"COPY ({select_sql}) TO ? (FORMAT PARQUET)", [str(path)]
        )
    finally:
        connection.close()


@pytest.fixture()
def bounded_real_fixture(
    tmp_path: Path,
) -> tuple[RealInputPaths, RealInputPins]:
    tables = tmp_path / "TABLES"
    tables.mkdir()
    campaigns = tables / "CAMPAIGNS.parquet"
    fills = tables / "FILL_SLICES.parquet"
    markouts = tables / "MARKOUTS.parquet"
    # Two scenario rows refer to one depletion episode.  This is deliberately
    # current-C1-shaped and therefore cannot become an A11 trigger row.
    write_parquet(
        campaigns,
        """
        SELECT * FROM (
          VALUES
          ('c1','e1',DATE '2026-07-12','KXTENNIS','EVENT-PROXY',
           'Tennis','yes',4000,1000000000,'PRIMARY',
           1050000000,1250000000,1040000000),
          ('c2','e1',DATE '2026-07-12','KXTENNIS','EVENT-PROXY',
           'Tennis','yes',4000,1000000000,'FAST',
           1005000000,1205000000,1004000000),
          ('c3','e2',DATE '2026-07-12','KXBASEBALL','EVENT-2',
           'Baseball','no',3000,2000000000,'PRIMARY',
           2050000000,2250000000,2040000000)
        ) AS t(
          campaign_id,episode_id,date,market_ticker,event_proxy,sport,side,
          quote_price_e4,depletion_ns,latency_id,activation_ns,
          cancel_effective_ns,state_ns
        )
        """,
    )
    write_parquet(
        fills,
        """
        SELECT * FROM (
          VALUES
          ('f1','c1',DATE '2026-07-12','Tennis','STRICT_THROUGH',10000),
          ('f2','c2',DATE '2026-07-12','Tennis','OPTIMISTIC_AT_TOUCH',10000),
          ('f3','c3',DATE '2026-07-12','Baseball','STRICT_THROUGH',10000)
        ) AS t(fill_slice_id,campaign_id,date,sport,track,fill_count_e4)
        """,
    )
    write_parquet(
        markouts,
        """
        SELECT * FROM (
          VALUES
          ('f1',DATE '2026-07-12','Tennis','STRICT_THROUGH',
           'OBSERVED_FULL',10000,0,-100),
          ('f1',DATE '2026-07-12','Tennis','STRICT_THROUGH',
           'CENSORED',0,10000,NULL),
          ('f2',DATE '2026-07-12','Tennis','OPTIMISTIC_AT_TOUCH',
           'OBSERVED_FULL',10000,0,100),
          ('f3',DATE '2026-07-12','Baseball','STRICT_THROUGH',
           'OBSERVED_FULL',10000,0,0)
        ) AS t(
          fill_slice_id,date,sport,track,markout_status,
          observed_count_e4,censored_count_e4,gross_e4
        )
        """,
    )

    ledger = tmp_path / "ANALYSIS_ARTIFACT_SHA256.json"
    artifacts = []
    for relative, path in (
        ("TABLES/CAMPAIGNS.parquet", campaigns),
        ("TABLES/FILL_SLICES.parquet", fills),
        ("TABLES/MARKOUTS.parquet", markouts),
    ):
        artifacts.append(
            {
                "path": relative,
                "sha256": sha(path),
                "bytes": path.stat().st_size,
            }
        )
    ledger.write_text(
        json.dumps(
            {
                "schema_version": "c1-analysis-artifact-ledger-v1",
                "artifacts": artifacts,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    quality = tmp_path / "DATA_QUALITY_RECEIPT.json"
    release_id = "2026-07-12__fixture"
    quality.write_text(
        json.dumps(
            {
                "schema_version": (
                    "deep03-d3-w2a-data-quality-receipt-v1"
                ),
                "releases": [
                    {
                        "date": "2026-07-12",
                        "release_id": release_id,
                        "l2_quality_summary": {
                            "lines": 10,
                            "parse_errors": 0,
                            "seq_gap_events": 0,
                            "seq_missed_total": 0,
                            "source_version_id": "version-1",
                        },
                        "quality_objects": [
                            {
                                "logical_key": (
                                    "control/quality/v1/date=2026-07-12/"
                                    "l2_gaps.json"
                                ),
                                "sha256": "a" * 64,
                                "source_version_id": "version-1",
                            }
                        ],
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    audit = tmp_path / "L2_INDEPENDENT_AUDIT_RECEIPT.json"
    audit.write_text(
        json.dumps(
            {
                "schema_version": (
                    "deep03-fullscope-l2-independent-audit-v2"
                ),
                "state": "PASS",
                "decision": "APPROVED_FOR_BASE_L2_INTEGRATION",
                "approved_claim_tier": "DESCRIPTIVE_ONLY_NO_PNL",
                "real_quality_receipts_assessed": True,
                "blockers": [],
                "input_release_ids": [release_id],
                "l2_quality_objects": [
                    {
                        "date": "2026-07-12",
                        "logical_key": (
                            "control/quality/v1/date=2026-07-12/"
                            "l2_gaps.json"
                        ),
                        "sha256": "a" * 64,
                        "source_version_id": "version-1",
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    paths = RealInputPaths(
        analysis_ledger=ledger,
        campaigns=campaigns,
        fill_slices=fills,
        markouts=markouts,
        deep03_data_quality=quality,
        deep03_l2_audit=audit,
    )
    pins = RealInputPins(
        analysis_ledger=sha(ledger),
        campaigns=sha(campaigns),
        fill_slices=sha(fills),
        markouts=sha(markouts),
        deep03_data_quality=sha(quality),
        deep03_l2_audit=sha(audit),
    )
    return paths, pins


def test_current_c1_shape_is_counted_but_not_promoted_to_net_pnl(
    bounded_real_fixture: tuple[RealInputPaths, RealInputPins],
):
    paths, pins = bounded_real_fixture
    result = audit_real_data(paths, pins)
    statuses = {row.experiment_id: row for row in result.experiments}

    assert result.classification == "ENGINEERING_INPUT_ONLY_NOT_NET_PNL"
    assert result.artifact_dates_utc == ("2026-07-12",)
    assert result.deep03_claim_tier == "DESCRIPTIVE_ONLY_NO_PNL"
    assert statuses["A01-SPREAD-CAPTURE"].source_candidate_rows == 0
    a11 = statuses["A11-ONE-SIDED-PROVISION"]
    assert a11.source_candidate_rows == 2
    assert a11.source_candidate_opportunities == 1
    assert a11.normalized_state_rows == 0
    assert a11.strict_fill_rows == 1
    assert a11.gross_markout_rows == 2
    assert a11.net_pnl_rows == 0
    assert "BLOCK_C1_MARKOUT_IS_NOT_EXACT_L2_EXIT" in a11.blocker_codes
    assert "BLOCK_A11_EVENT_PROXY_IS_NOT_ROOT_EVENT_ID" in a11.blocker_codes
    b09 = statuses["B09-LISTING-TO-START-DRIFT"]
    assert b09.source_candidate_rows == 0
    assert "BLOCK_B09_TRAIN_ARTIFACT_UNBOUND" in b09.blocker_codes
    assert all(row.net_pnl_rows == 0 for row in statuses.values())
    assert all(row.exact_exit_rows == 0 for row in statuses.values())
    assert result.sha256 == result.sha256


def test_every_input_is_hash_bound_and_symlinks_are_rejected(
    bounded_real_fixture: tuple[RealInputPaths, RealInputPins],
    tmp_path: Path,
):
    paths, pins = bounded_real_fixture
    with pytest.raises(RealDataError, match="SHA-256 mismatch"):
        audit_real_data(paths, replace(pins, campaigns="0" * 64))

    link = tmp_path / "campaigns-link.parquet"
    link.symlink_to(paths.campaigns)
    linked_paths = replace(paths, campaigns=link)
    with pytest.raises(RealDataError, match="symlink"):
        audit_real_data(linked_paths, pins)


def test_ledger_cannot_disagree_with_a_separately_pinned_table(
    bounded_real_fixture: tuple[RealInputPaths, RealInputPins],
):
    paths, pins = bounded_real_fixture
    document = json.loads(paths.analysis_ledger.read_text(encoding="utf-8"))
    document["artifacts"][0]["bytes"] += 1
    paths.analysis_ledger.write_text(
        json.dumps(document, sort_keys=True), encoding="utf-8"
    )
    updated = replace(pins, analysis_ledger=sha(paths.analysis_ledger))
    with pytest.raises(RealDataError, match="ledger contradicts"):
        audit_real_data(paths, updated)


def test_a11_direct_mapper_requires_fields_instead_of_inventing_them(
    bounded_real_fixture: tuple[RealInputPaths, RealInputPins],
):
    paths, pins = bounded_real_fixture
    rows, blockers = load_a11_normalized_rows(
        paths.campaigns, pins.campaigns
    )
    assert rows == ()
    assert "BLOCK_A11_MISSING_DIRECT_FIELD:root_event_id" in blockers
    assert "BLOCK_A11_MISSING_DIRECT_FIELD:scheduled_start_ts_ns" in blockers
    assert "BLOCK_A11_MISSING_DIRECT_FIELD:tick_size_e4" in blockers


def test_a11_enriched_direct_fields_map_without_inference(tmp_path: Path):
    path = tmp_path / "enriched.parquet"
    write_parquet(
        path,
        """
        SELECT
          'campaign-1'::VARCHAR AS campaign_id,
          'root-1'::VARCHAR AS root_event_id,
          'KXTEST'::VARCHAR AS market_ticker,
          'Tennis'::VARCHAR AS sport,
          1000000000000::BIGINT AS decision_ts_ns,
          999999000000::BIGINT AS features_asof_ns,
          999999000000::BIGINT AS book_observed_at_ns,
          100::BIGINT AS tick_size_e4,
          1060000000000::BIGINT AS scheduled_start_ts_ns,
          999999000000::BIGINT AS scheduled_start_asof_ns,
          4000::BIGINT AS best_yes_bid_e4,
          NULL::BIGINT AS best_yes_ask_e4,
          970000000000::BIGINT AS state_started_at_ns,
          4200::BIGINT AS reference_mid_onset_e4,
          969999000000::BIGINT AS reference_mid_onset_observed_at_ns,
          4000::BIGINT AS surviving_side_onset_e4,
          5::BIGINT AS update_count_60s,
          1::BIGINT AS trade_count_300s,
          false::BOOLEAN AS activity_burst,
          true::BOOLEAN AS book_valid,
          true::BOOLEAN AS lifecycle_open,
          false::BOOLEAN AS gap,
          false::BOOLEAN AS paused,
          false::BOOLEAN AS locked,
          false::BOOLEAN AS crossed
        """,
    )
    rows, blockers = load_a11_normalized_rows(path, sha(path))
    assert blockers == ()
    assert len(rows) == 1
    assert rows[0].row_id == "campaign-1"
    assert rows[0].root_event_id == "root-1"
    assert rows[0].scheduled_start_ts_ns == 1_060_000_000_000
    assert rows[0].best_yes_ask_e4 is None


def test_exact_20260722_artifacts_remain_inventory_only():
    c1 = Path(
        "/Users/ritcardo/HFT-BOT-c1-real-fill-01/tmp/c1_real_fill_run/"
        "c1-real-fill-20260722-214710-8a62e78/work/results"
    )
    deep03 = ROOT / "Deepresearch V3" / "run_2026-07-22_D3-W2A"
    if not c1.is_dir():
        pytest.skip("exact local C1 artifact bundle is not materialized")
    paths = RealInputPaths(
        analysis_ledger=c1 / "ANALYSIS_ARTIFACT_SHA256.json",
        campaigns=c1 / "TABLES" / "CAMPAIGNS.parquet",
        fill_slices=c1 / "TABLES" / "FILL_SLICES.parquet",
        markouts=c1 / "TABLES" / "MARKOUTS.parquet",
        deep03_data_quality=deep03 / "DATA_QUALITY_RECEIPT.json",
        deep03_l2_audit=deep03 / "L2_INDEPENDENT_AUDIT_RECEIPT.json",
    )
    result = audit_real_data(paths, RealInputPins.known_20260722())
    status = {row.experiment_id: row for row in result.experiments}
    assert result.artifact_dates_utc == (
        "2026-07-12",
        "2026-07-15",
        "2026-07-17",
    )
    inventory = {row.source_id: row for row in result.source_inventory}
    assert inventory["C1_CAMPAIGNS"].row_count == 1_116_054
    assert status["A01-SPREAD-CAPTURE"].source_candidate_rows == 0
    assert (
        status["A11-ONE-SIDED-PROVISION"].source_candidate_opportunities
        == 164_295
    )
    assert status["A11-ONE-SIDED-PROVISION"].strict_fill_rows == 28_680
    assert status["A11-ONE-SIDED-PROVISION"].gross_markout_rows == 114_720
    assert all(row.normalized_state_rows == 0 for row in status.values())
    assert all(row.net_pnl_rows == 0 for row in status.values())
