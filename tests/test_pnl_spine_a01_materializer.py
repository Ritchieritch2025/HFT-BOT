"""Causal and fail-closed tests for the A01 historical materializer."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.research.pnl_spine.a01_materializer import (  # noqa: E402
    A01CollectionCoverage,
    A01GateThreshold,
    A01SourceCoverage,
    A01TrainingArtifact,
    SOURCE_COVERAGE_SCHEMA,
    TRAIN_ARTIFACT_SCHEMA,
    TRAIN_DATES,
    VALIDATION_DATES,
    a01_extraction_spec,
    a01_opportunity_schedule_sha256,
    fit_a01_training_artifact,
    materialize_a01,
)
from tools.research.pnl_spine import a01_materialize_cli  # noqa: E402
from tools.research.pnl_spine.contracts import canonical_sha256  # noqa: E402
from tools.research.pnl_spine.experiments import (  # noqa: E402
    DecisionStatus,
    FrozenExperimentAdapters,
    NormalizedStateRow,
    RuntimeBindings,
)


FREEZE_PATH = (
    ROOT
    / "Deepresearch V3"
    / "registry"
    / "frozen"
    / "PNL_SPINE_EXPERIMENTS_V1.json"
)
SECOND = 1_000_000_000
MILLISECOND = 1_000_000
H_A = "a" * 64
H_B = "b" * 64
H_C = "c" * 64
H_D = "d" * 64


def freeze() -> dict[str, Any]:
    return json.loads(FREEZE_PATH.read_text(encoding="utf-8"))


def utc_ns(date: str, hour: int = 12) -> int:
    return int(
        datetime.fromisoformat(
            f"{date}T{hour:02d}:00:00+00:00"
        ).timestamp()
    ) * SECOND


def metadata(
    date: str,
    market: str,
    *,
    root: str = "ROOT-1",
    sport: str = "Tennis",
    decision_ns: int | None = None,
) -> dict[str, Any]:
    decision_ns = utc_ns(date) if decision_ns is None else decision_ns
    return {
        "date": date,
        "market_ticker": market,
        "root_event_id": root,
        "sport": sport,
        "tick_size_e4": 100,
        "scheduled_start_ts_ns": decision_ns + 2 * 60 * 60 * SECOND,
        "scheduled_start_asof_ns": decision_ns - SECOND,
        "identity_asof_ns": decision_ns - SECOND,
        "tick_size_asof_ns": decision_ns - SECOND,
        "lifecycle_asof_ns": decision_ns - SECOND,
        "market_structure_asof_ns": decision_ns - SECOND,
        "lifecycle_open": True,
        "paused": False,
        "standard_binary_clob": True,
        "mve": False,
        "source_sha256": H_B,
    }


def book(
    date: str,
    market: str,
    timestamp_ns: int,
    sequence: int,
    *,
    snapshot: bool = False,
    epoch: int = 1,
    sid: int = 7,
    gap: bool = False,
    reset: bool | None = None,
    bid: int = 4_000,
    ask: int = 4_400,
    toxicity: int = 20,
    adverse: int = 20,
    liquidity: str = "LIQ-A",
) -> dict[str, Any]:
    return {
        "record_id": f"{date}|{market}|{timestamp_ns}|{sequence}",
        "date": date,
        "market_ticker": market,
        "receive_timestamp_ns": timestamp_ns,
        "receive_timestamp_us": timestamp_ns // 1_000,
        "receive_monotonic_ns": timestamp_ns,
        "stream_epoch": 1,
        "ws_sid": sid,
        "ws_seq": sequence,
        "sid_gap_generation": 0,
        "market_revalidated_gap_generation": 0,
        "sid_sequence_valid": True,
        "classification": (
            "SNAPSHOT_APPLIED" if snapshot else "DELTA_APPLIED"
        ),
        "snapshot_epoch": epoch,
        "book_valid": not gap,
        "topology": "TWO_SIDED" if not gap else "INVALID_EPOCH",
        "best_yes_bid_e4": bid,
        "best_yes_ask_e4": ask,
        "yes_bids": [
            {"yes_price_e4": bid, "quantity_e4": 30_000},
            {"yes_price_e4": bid - 100, "quantity_e4": 20_000},
        ],
        "yes_asks": [
            {"yes_price_e4": ask, "quantity_e4": 40_000},
            {"yes_price_e4": ask + 100, "quantity_e4": 10_000},
        ],
        "gap": gap,
        "reset": snapshot if reset is None else reset,
        "toxicity_score_e6": toxicity,
        "toxicity_asof_ns": timestamp_ns,
        "adverse_width_e4": adverse,
        "adverse_width_asof_ns": timestamp_ns,
        "liquidity_stratum": liquidity,
        "liquidity_stratum_asof_ns": timestamp_ns,
        "depth_complete": True,
        "replay_receipt_sha256": H_A,
        "source_sha256": H_A,
    }


def continuous_books(
    date: str,
    market: str,
    start_ns: int,
    *,
    duration_ms: int = 125_250,
    step_ms: int = 250,
    epoch: int = 1,
    sid: int = 7,
) -> list[dict[str, Any]]:
    return [
        book(
            date,
            market,
            start_ns + offset_ms * MILLISECOND,
            index + 1,
            snapshot=index == 0,
            epoch=epoch,
            sid=sid,
        )
        for index, offset_ms in enumerate(
            range(0, duration_ms + 1, step_ms)
        )
    ]


def threshold_artifact(
    *,
    liquidity: str = "LIQ-A",
) -> A01TrainingArtifact:
    document = freeze()
    return A01TrainingArtifact(
        schema_version=TRAIN_ARTIFACT_SCHEMA,
        freeze_sha256=document["freeze_sha256"],
        train_dates_utc=TRAIN_DATES,
        quantile_rule="NEAREST_RANK_CEIL_V1",
        source_manifest_sha256=H_D,
        thresholds=(
            A01GateThreshold(
                stratum_key=(
                    f"Tennis|PREMATCH_15M_TO_6H|{liquidity}"
                ),
                sample_count=10,
                activity_p75=1,
                toxicity_p90_e6=100,
                adverse_width_p90_e4=100,
            ),
        ),
    )


def selection(
    date: str,
    market: str,
    decision_ns: int,
    *,
    root: str = "ROOT-1",
    score: int = 500,
    loss: int = 1_000,
    margin_ticks: int = 3,
) -> dict[str, Any]:
    return {
        "selection_id": f"selection|{date}|{market}|{decision_ns}",
        "date": date,
        "root_event_id": root,
        "market_ticker": market,
        "decision_ts_ns": decision_ns,
        "risk_adjusted_score_e6": score,
        "worst_state_loss_e6": loss,
        "net_capture_margin_ticks": margin_ticks,
        "asof_ns": decision_ns,
        "fee_facts_sha256": H_A,
        "measured_latency_receipt_sha256": H_B,
        "selection_policy_sha256": H_C,
        "source_sha256": H_C,
    }


def opportunity(
    date: str,
    root: str,
    decision_ns: int,
    *,
    suffix: str = "1",
) -> dict[str, Any]:
    return {
        "opportunity_id": (
            f"opportunity|{date}|{root}|{decision_ns}|{suffix}"
        ),
        "date": date,
        "root_event_id": root,
        "decision_ts_ns": decision_ns,
        "asof_ns": decision_ns,
        "opportunity_policy_sha256": H_D,
        "source_sha256": H_C,
    }


def opportunities_for(
    selections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    keys = sorted(
        {
            (
                str(row["date"]),
                str(row["root_event_id"]),
                int(row["decision_ts_ns"]),
            )
            for row in selections
        }
    )
    return [
        opportunity(date, root, decision)
        for date, root, decision in keys
    ]


def trade(
    date: str,
    market: str,
    timestamp_ns: int,
    *,
    suffix: str = "1",
) -> dict[str, Any]:
    receive_us = timestamp_ns // 1_000
    return {
        "trade_id": f"trade-{market}-{suffix}",
        "date": date,
        "market_ticker": market,
        "exchange_timestamp_us": receive_us - 1_000,
        "receive_timestamp_us": receive_us,
        "receive_wall_ns": timestamp_ns,
        "receive_monotonic_ns": timestamp_ns,
        "clock_domain": "LOCAL_RECEIVE_WALL",
        "yes_price_e4": 3_800,
        "quantity_e4": 10_000,
        "taker_side": "no",
        "source_sha256": H_A,
    }


def exit_request(
    date: str,
    market: str,
    decision_ns: int,
    *,
    suffix: str = "1",
    latency_ms: int = 100,
) -> dict[str, Any]:
    return {
        "exit_id": f"exit-{market}-{suffix}",
        "date": date,
        "market_ticker": market,
        "exit_decision_ts_ns": decision_ns,
        "effective_ts_ns": decision_ns + latency_ms * MILLISECOND,
        "measured_ioc_exit_latency_ns": latency_ms * MILLISECOND,
        "maximum_snapshot_age_us": 250_000,
        "measured_latency_receipt_sha256": H_B,
        "source_sha256": H_C,
    }


def settlement(market: str) -> dict[str, Any]:
    return {
        "settlement_id": f"settlement-{market}",
        "market_ticker": market,
        "status": "FINALIZED",
        "finalized": True,
        "yes_settlement_value_e4": 10_000,
        "observed_at_ns": utc_ns("2026-07-18"),
        "revision": 1,
        "source_sha256": H_D,
    }


def materialize_complete(
    *,
    books: list[dict[str, Any]],
    metadata_rows: list[dict[str, Any]],
    selections: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    exits: list[dict[str, Any]],
    settlements: list[dict[str, Any]],
    opportunities: list[dict[str, Any]] | None = None,
):
    opportunity_rows = (
        opportunities_for(selections)
        if opportunities is None
        else opportunities
    )
    artifact = threshold_artifact()

    def run(exit_rows: list[dict[str, Any]]):
        coverage = source_coverage(
            stage="VALIDATION",
            books=books,
            metadata_rows=metadata_rows,
            selections=selections,
            opportunities=opportunity_rows,
            trades=trades,
            exits=exit_rows,
            settlements=settlements,
        )
        return materialize_a01(
            stage="VALIDATION",
            freeze_document=freeze(),
            training_artifact=artifact,
            expected_training_artifact_sha256=artifact.sha256,
            source_coverage=coverage,
            expected_source_coverage_sha256=coverage.sha256,
            expected_opportunity_policy_sha256=(
                coverage.opportunity_policy_sha256
            ),
            expected_fee_facts_sha256=coverage.fee_facts_sha256,
            expected_measured_latency_receipt_sha256=(
                coverage.measured_latency_receipt_sha256
            ),
            expected_selection_policy_sha256=(
                coverage.selection_policy_sha256
            ),
            book_records=books,
            metadata_records=metadata_rows,
            opportunity_records=opportunity_rows,
            selection_records=selections,
            public_trade_records=trades,
            exit_requests=exit_rows,
            settlement_records=settlements,
        )

    if any("intent_id" in row for row in exits):
        return run(exits)
    preflight = run([])
    bindings = RuntimeBindings(
        fee_facts_sha256=H_A,
        latency_receipt_sha256=H_B,
        root_map_sha256=H_C,
        scheduled_start_source_sha256=H_D,
        risk_policy_sha256="e" * 64,
        terminal_contract_sha256="f" * 64,
        strict_fill_evidence_sha256="0" * 64,
        card_parameter_artifact_sha256=artifact.sha256,
    )
    adapter = FrozenExperimentAdapters(freeze())
    expanded: list[dict[str, Any]] = []
    for row in preflight.rows:
        decision = adapter.evaluate_a01(
            NormalizedStateRow(**row),
            bindings,
        )
        if decision.status is not DecisionStatus.ORDER_INTENTS:
            continue
        templates = [
            template
            for template in exits
            if template["market_ticker"] == row["market_ticker"]
            and template["date"]
            == datetime.fromtimestamp(
                row["decision_ts_ns"] // SECOND,
                tz=timezone.utc,
            ).date().isoformat()
        ]
        if not templates:
            continue
        template = templates[0]
        for intent in decision.intents:
            exit_decision_ts_ns = (
                row["decision_ts_ns"]
                + intent.expire_after_ms * MILLISECOND
                + 100 * MILLISECOND
            )
            expanded.append(
                {
                    **template,
                    "exit_id": (
                        f"{template['exit_id']}|{intent.intent_id}"
                    ),
                    "row_id": row["row_id"],
                    "intent_id": intent.intent_id,
                    "exit_decision_ts_ns": exit_decision_ts_ns,
                    "effective_ts_ns": (
                        exit_decision_ts_ns + 100 * MILLISECOND
                    ),
                    "measured_ioc_exit_latency_ns": (
                        100 * MILLISECOND
                    ),
                    "exit_limit_price_e4": (
                        100
                        if intent.side.value == "BUY"
                        else 9_900
                    ),
                }
            )
    return run(expanded)


def source_coverage(
    *,
    stage: str,
    books: list[dict[str, Any]],
    metadata_rows: list[dict[str, Any]],
    selections: list[dict[str, Any]],
    opportunities: list[dict[str, Any]] | None = None,
    trades: list[dict[str, Any]],
    exits: list[dict[str, Any]],
    settlements: list[dict[str, Any]],
) -> A01SourceCoverage:
    opportunity_rows = (
        opportunities_for(selections)
        if opportunities is None
        else opportunities
    )
    collections = {
        "book_records": books,
        "exit_requests": exits,
        "metadata_records": metadata_rows,
        "opportunity_records": opportunity_rows,
        "public_trade_records": trades,
        "selection_records": selections,
        "settlement_records": settlements,
    }
    return A01SourceCoverage(
        schema_version=SOURCE_COVERAGE_SCHEMA,
        stage=stage,
        cohort_dates_utc=(
            TRAIN_DATES if stage == "TRAIN" else VALIDATION_DATES
        ),
        opportunity_policy_sha256=H_D,
        opportunity_schedule_sha256=(
            a01_opportunity_schedule_sha256(opportunity_rows)
        ),
        fee_facts_sha256=H_A,
        measured_latency_receipt_sha256=H_B,
        selection_policy_sha256=H_C,
        root_map_sha256=H_C,
        scheduled_start_source_sha256=H_D,
        risk_policy_sha256="e" * 64,
        terminal_contract_sha256="f" * 64,
        strict_fill_evidence_sha256="0" * 64,
        place_latency_ns=100 * MILLISECOND,
        cancel_latency_ns=100 * MILLISECOND,
        ioc_exit_latency_ns=100 * MILLISECOND,
        collections=tuple(
            A01CollectionCoverage(
                collection=name,
                manifest_sha256=H_A,
                records_sha256=canonical_sha256(
                    [dict(row) for row in records]
                ),
                record_count=len(records),
            )
            for name, records in sorted(collections.items())
        ),
    )


def blocker_codes(result: Any) -> set[str]:
    return {row["code"] for row in result.blockers}


def test_train_fit_uses_07_12_15_only_and_never_opens_validation():
    records = [
        book("2026-07-12", "MKT-A", utc_ns("2026-07-12"), 1, snapshot=True),
        book(
            "2026-07-15",
            "MKT-B",
            utc_ns("2026-07-15"),
            1,
            snapshot=True,
            toxicity=90,
            adverse=80,
        ),
    ]
    metadata_rows = [
        metadata("2026-07-12", "MKT-A"),
        metadata("2026-07-15", "MKT-B"),
    ]
    fitted = fit_a01_training_artifact(
        freeze_document=freeze(),
        book_records=records,
        metadata_records=metadata_rows,
        source_manifest_sha256=H_D,
    )

    assert fitted.ready
    assert fitted.artifact is not None
    assert fitted.artifact.train_dates_utc == TRAIN_DATES
    assert fitted.artifact.thresholds == (
        A01GateThreshold(
            stratum_key=(
                "Tennis|PREMATCH_15M_TO_6H|LIQ-A"
            ),
            sample_count=2,
            activity_p75=1,
            toxicity_p90_e6=90,
            adverse_width_p90_e4=80,
        ),
    )

    validation_record = book(
        "2026-07-17",
        "MKT-V",
        utc_ns("2026-07-17"),
        1,
        snapshot=True,
    )
    blocked = fit_a01_training_artifact(
        freeze_document=freeze(),
        book_records=records + [validation_record],
        metadata_records=metadata_rows
        + [metadata("2026-07-17", "MKT-V")],
        source_manifest_sha256=H_D,
    )
    assert blocked.artifact is None
    assert (
        "BLOCK_A01_VALIDATION_EXPOSED_DURING_TRAIN"
        in blocker_codes(blocked)
    )


def test_validation_materializes_runner_inputs_and_tie_breaks_market():
    date = "2026-07-17"
    start = utc_ns(date)
    decision = start + 120 * SECOND
    books_a = continuous_books(date, "MKT-A", start)
    books_b = continuous_books(date, "MKT-B", start, sid=8)
    result = materialize_complete(
        # Reverse input proves receive-time ordering does not trust file order.
        books=list(reversed(books_a + books_b)),
        metadata_rows=[
            metadata(date, "MKT-A", decision_ns=decision),
            metadata(date, "MKT-B", decision_ns=decision),
        ],
        selections=[
            selection(date, "MKT-B", decision, score=500, loss=1_000),
            selection(date, "MKT-A", decision, score=500, loss=1_000),
        ],
        trades=[
            trade(date, "MKT-B", decision + 2 * SECOND),
            trade(date, "MKT-A", decision + SECOND),
        ],
        exits=[exit_request(date, "MKT-A", decision)],
        settlements=[settlement("MKT-A")],
    )

    assert result.ready
    assert result.blockers == ()
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row["market_ticker"] == "MKT-A"
    assert row["warmup_started_at_ns"] == start
    assert row["state_started_at_ns"] == start
    assert row["update_count_60s"] == 240
    assert row["activity_gate_passed"] is True
    assert row["toxicity_gate_passed"] is True
    assert row["adverse_width_gate_passed"] is True
    assert [trade["trade_id"] for trade in result.public_trades] == [
        "trade-MKT-A-1"
    ]
    assert len(result.exit_snapshots) == 1
    snapshot = result.exit_snapshots[0]
    assert snapshot["market_ticker"] == "MKT-A"
    assert snapshot["yes_bids"] == books_a[-1]["yes_bids"]
    assert snapshot["yes_asks"] == books_a[-1]["yes_asks"]
    assert len(result.closures) == 2
    assert all(
        row["settlement_id"] == "settlement-MKT-A"
        for row in result.closures
    )
    assert all(
        row["exit_snapshot_id"] == snapshot["snapshot_id"]
        for row in result.closures
    )
    assert all(
        row["exit_decision_ts_ns"]
        == decision + 5_100 * MILLISECOND
        for row in result.closures
    )
    assert len(result.settlements) == 1
    assert result.settlements[0]["status"] == "FINALIZED"

    bindings = RuntimeBindings(
        fee_facts_sha256=H_A,
        latency_receipt_sha256=H_B,
        root_map_sha256=H_C,
        scheduled_start_source_sha256=H_D,
        risk_policy_sha256="e" * 64,
        terminal_contract_sha256="f" * 64,
        strict_fill_evidence_sha256="0" * 64,
        card_parameter_artifact_sha256=(
            result.training_artifact_sha256
        ),
    )
    decision_result = FrozenExperimentAdapters(
        freeze()
    ).evaluate_a01(
        NormalizedStateRow(**row),
        bindings,
    )
    assert decision_result.status is DecisionStatus.ORDER_INTENTS
    assert len(decision_result.intents) == 2
    assert all(intent.post_only for intent in decision_result.intents)
    assert all(
        intent.quantity_e4 == 10_000
        for intent in decision_result.intents
    )


def test_250ms_boundary_is_inclusive_and_one_nanosecond_late_blocks():
    date = "2026-07-17"
    observed = utc_ns(date)
    books = [book(date, "MKT-A", observed, 1, snapshot=True)]
    meta = metadata(date, "MKT-A", decision_ns=observed)
    accepted_decision = observed + 250 * MILLISECOND
    accepted_meta = {
        **meta,
        "scheduled_start_ts_ns": accepted_decision
        + 2 * 60 * 60 * SECOND,
    }
    accepted = materialize_complete(
        books=books,
        metadata_rows=[accepted_meta],
        selections=[selection(date, "MKT-A", accepted_decision)],
        trades=[],
        exits=[
            exit_request(
                date,
                "MKT-A",
                accepted_decision,
                latency_ms=0,
            )
        ],
        settlements=[settlement("MKT-A")],
    )
    assert "BLOCK_A01_DECISION_BOOK_STALE_250MS" not in blocker_codes(
        accepted
    )
    assert len(accepted.rows) == 1

    late_decision = accepted_decision + 1
    late_meta = {
        **meta,
        "scheduled_start_ts_ns": late_decision
        + 2 * 60 * 60 * SECOND,
    }
    late = materialize_complete(
        books=books,
        metadata_rows=[late_meta],
        selections=[selection(date, "MKT-A", late_decision)],
        trades=[],
        exits=[],
        settlements=[],
    )
    assert "BLOCK_A01_DECISION_BOOK_STALE_250MS" in blocker_codes(late)
    assert late.rows == ()


def test_stale_gap_resets_both_warmup_and_spread_dwell():
    date = "2026-07-17"
    start = utc_ns(date)
    before = continuous_books(
        date,
        "MKT-A",
        start,
        duration_ms=5_000,
    )
    after_start = before[-1]["receive_timestamp_ns"] + (
        251 * MILLISECOND
    )
    after = [
        book(
            date,
            "MKT-A",
            after_start + offset * 250 * MILLISECOND,
            len(before) + offset + 1,
            epoch=1,
        )
        for offset in range(0, 22)
    ]
    decision = after[-1]["receive_timestamp_ns"]
    meta = metadata(date, "MKT-A", decision_ns=decision)
    result = materialize_complete(
        books=list(reversed(before + after)),
        metadata_rows=[meta],
        selections=[selection(date, "MKT-A", decision)],
        trades=[],
        exits=[exit_request(date, "MKT-A", decision)],
        settlements=[settlement("MKT-A")],
    )
    assert result.ready
    assert result.rows[0]["warmup_started_at_ns"] == after_start
    assert result.rows[0]["state_started_at_ns"] == after_start


def test_spread_dwell_is_exactly_five_continuous_receive_seconds():
    date = "2026-07-17"
    start = utc_ns(date)

    def run_at(offset_ms: int):
        offsets = list(range(0, offset_ms, 250)) + [offset_ms]
        records = [
            book(
                date,
                "MKT-A",
                start + offset * MILLISECOND,
                index + 1,
                snapshot=index == 0,
            )
            for index, offset in enumerate(offsets)
        ]
        decision = records[-1]["receive_timestamp_ns"]
        return materialize_complete(
            books=records,
            metadata_rows=[
                metadata(date, "MKT-A", decision_ns=decision)
            ],
            selections=[selection(date, "MKT-A", decision)],
            trades=[],
            exits=[exit_request(date, "MKT-A", decision)],
            settlements=[settlement("MKT-A")],
        )

    before = run_at(4_999)
    exact = run_at(5_000)
    assert before.ready and exact.ready
    assert (
        before.rows[0]["decision_ts_ns"]
        - before.rows[0]["state_started_at_ns"]
        == 4_999 * MILLISECOND
    )
    assert (
        exact.rows[0]["decision_ts_ns"]
        - exact.rows[0]["state_started_at_ns"]
        == 5_000 * MILLISECOND
    )

    bindings = RuntimeBindings(
        fee_facts_sha256=H_A,
        latency_receipt_sha256=H_B,
        root_map_sha256=H_C,
        scheduled_start_source_sha256=H_D,
        risk_policy_sha256="e" * 64,
        terminal_contract_sha256="f" * 64,
        strict_fill_evidence_sha256="0" * 64,
        card_parameter_artifact_sha256=H_D,
    )
    adapter = FrozenExperimentAdapters(freeze())
    before_decision = adapter.evaluate_a01(
        NormalizedStateRow(**before.rows[0]),
        bindings,
    )
    exact_decision = adapter.evaluate_a01(
        NormalizedStateRow(**exact.rows[0]),
        bindings,
    )
    assert before_decision.reason_codes == (
        "ABSTAIN_A01_SPREAD_DWELL_BELOW_5S",
    )
    # At exactly five seconds the dwell gate has passed.  The deliberately
    # short fixture then stops at the separate 120-second warm-up gate.
    assert exact_decision.reason_codes == (
        "ABSTAIN_A01_WARMUP_INCOMPLETE",
    )


def test_epoch_change_without_snapshot_fails_closed_and_snapshot_resets():
    date = "2026-07-17"
    start = utc_ns(date)
    records = [
        book(date, "MKT-A", start, 1, snapshot=True, epoch=1),
        book(
            date,
            "MKT-A",
            start + 250 * MILLISECOND,
            2,
            snapshot=False,
            epoch=2,
        ),
    ]
    decision = records[-1]["receive_timestamp_ns"]
    blocked = materialize_complete(
        books=records,
        metadata_rows=[
            metadata(date, "MKT-A", decision_ns=decision)
        ],
        selections=[selection(date, "MKT-A", decision)],
        trades=[],
        exits=[],
        settlements=[],
    )
    assert (
        "BLOCK_A01_DECISION_BOOK_GAP_RESET_EPOCH"
        in blocker_codes(blocked)
    )
    assert blocked.rows == ()

    restored_ns = decision + 250 * MILLISECOND
    restored = records + [
        book(
            date,
            "MKT-A",
            restored_ns,
            3,
            snapshot=True,
            epoch=2,
        )
    ]
    recovered = materialize_complete(
        books=restored,
        metadata_rows=[
            metadata(date, "MKT-A", decision_ns=restored_ns)
        ],
        selections=[selection(date, "MKT-A", restored_ns)],
        trades=[],
        exits=[exit_request(date, "MKT-A", restored_ns)],
        settlements=[settlement("MKT-A")],
    )
    assert recovered.ready
    assert recovered.rows[0]["warmup_started_at_ns"] == restored_ns
    assert recovered.rows[0]["state_started_at_ns"] == restored_ns


def test_missing_direct_sources_are_machine_blockers_not_filled_values():
    date = "2026-07-17"
    now = utc_ns(date)
    missing_schedule = metadata(date, "MKT-A")
    missing_schedule.pop("scheduled_start_ts_ns")
    result = materialize_a01(
        stage="VALIDATION",
        freeze_document=freeze(),
        training_artifact=threshold_artifact(),
        expected_training_artifact_sha256=threshold_artifact().sha256,
        source_coverage=source_coverage(
            stage="VALIDATION",
            books=[book(date, "MKT-A", now, 1, snapshot=True)],
            metadata_rows=[missing_schedule],
            selections=[],
            trades=[],
            exits=[],
            settlements=[],
        ),
        expected_source_coverage_sha256=source_coverage(
            stage="VALIDATION",
            books=[book(date, "MKT-A", now, 1, snapshot=True)],
            metadata_rows=[missing_schedule],
            selections=[],
            trades=[],
            exits=[],
            settlements=[],
        ).sha256,
        expected_opportunity_policy_sha256=H_D,
        expected_fee_facts_sha256=H_A,
        expected_measured_latency_receipt_sha256=H_B,
        expected_selection_policy_sha256=H_C,
        book_records=[book(date, "MKT-A", now, 1, snapshot=True)],
        metadata_records=[missing_schedule],
        opportunity_records=[],
        selection_records=[],
        public_trade_records=[],
        exit_requests=[],
        settlement_records=[],
    )
    codes = blocker_codes(result)
    assert "BLOCK_A01_METADATA_FIELD_MISSING" in codes
    assert "BLOCK_A01_SELECTION_SCORE_AUTHORITY_MISSING" in codes
    assert result.rows == ()
    assert result.exit_snapshots == ()
    assert result.settlements == ()

    spec = a01_extraction_spec()
    assert spec["record_sources"]["settlements"]["current_stage"] is None
    assert (
        spec["record_sources"]["exit_snapshots"][
            "forbidden_substitution"
        ]
        == "l2_replay top/depth3 columns are not a full IOC walk"
    )
    assert any(
        "scenario" in text
        for text in spec["forbidden_substitutions"]
    )


def test_validation_rejects_non_train_or_wrong_freeze_artifact():
    artifact = threshold_artifact()
    with pytest.raises(
        Exception,
        match="training artifact must use",
    ):
        replace(
            artifact,
            train_dates_utc=("2026-07-12", "2026-07-17"),
        )

    wrong_freeze = replace(
        artifact,
        freeze_sha256="0" * 64,
    )
    date = "2026-07-17"
    now = utc_ns(date)
    result = materialize_a01(
        stage="VALIDATION",
        freeze_document=freeze(),
        training_artifact=wrong_freeze,
        expected_training_artifact_sha256=wrong_freeze.sha256,
        source_coverage=source_coverage(
            stage="VALIDATION",
            books=[book(date, "MKT-A", now, 1, snapshot=True)],
            metadata_rows=[metadata(date, "MKT-A")],
            selections=[selection(date, "MKT-A", now)],
            trades=[],
            exits=[],
            settlements=[],
        ),
        expected_source_coverage_sha256=source_coverage(
            stage="VALIDATION",
            books=[book(date, "MKT-A", now, 1, snapshot=True)],
            metadata_rows=[metadata(date, "MKT-A")],
            selections=[selection(date, "MKT-A", now)],
            trades=[],
            exits=[],
            settlements=[],
        ).sha256,
        expected_opportunity_policy_sha256=H_D,
        expected_fee_facts_sha256=H_A,
        expected_measured_latency_receipt_sha256=H_B,
        expected_selection_policy_sha256=H_C,
        book_records=[book(date, "MKT-A", now, 1, snapshot=True)],
        metadata_records=[metadata(date, "MKT-A")],
        opportunity_records=[
            opportunity(date, "ROOT-1", now)
        ],
        selection_records=[selection(date, "MKT-A", now)],
        public_trade_records=[],
        exit_requests=[],
        settlement_records=[],
    )
    assert (
        "BLOCK_A01_TRAIN_ARTIFACT_FREEZE_MISMATCH"
        in blocker_codes(result)
    )


def test_partition_dates_are_bound_to_causal_utc_timestamps():
    forged = book(
        "2026-07-12",
        "MKT-A",
        utc_ns("2026-07-17"),
        1,
        snapshot=True,
    )
    fitted = fit_a01_training_artifact(
        freeze_document=freeze(),
        book_records=[
            forged,
            book(
                "2026-07-15",
                "MKT-B",
                utc_ns("2026-07-15"),
                1,
                snapshot=True,
            ),
        ],
        metadata_records=[
            metadata("2026-07-12", "MKT-A"),
            metadata("2026-07-15", "MKT-B"),
        ],
        source_manifest_sha256=H_D,
    )
    assert fitted.artifact is None
    assert "BLOCK_A01_L2_RECORD_INVALID" in blocker_codes(fitted)


def test_same_wall_and_monotonic_receive_clock_is_ambiguous():
    date = "2026-07-17"
    now = utc_ns(date)
    first = book(date, "MKT-A", now, 1, snapshot=True, sid=7)
    second = book(
        date,
        "MKT-A",
        now,
        1,
        snapshot=True,
        sid=8,
        bid=3_900,
        ask=4_300,
    )
    second["record_id"] = "same-clocks-different-sid"
    second["receive_monotonic_ns"] = first["receive_monotonic_ns"]
    result = materialize_complete(
        books=[first, second],
        metadata_rows=[metadata(date, "MKT-A", decision_ns=now)],
        selections=[selection(date, "MKT-A", now)],
        trades=[],
        exits=[],
        settlements=[],
    )
    assert "BLOCK_A01_RECEIVE_ORDER_AMBIGUOUS" in blocker_codes(result)
    assert result.rows == ()


def test_gap_cannot_recover_via_delta_without_new_snapshot_epoch():
    date = "2026-07-17"
    now = utc_ns(date)
    records = [
        book(date, "MKT-A", now, 1, snapshot=True, epoch=1),
        book(
            date,
            "MKT-A",
            now + 250 * MILLISECOND,
            2,
            gap=True,
            epoch=1,
        ),
        book(
            date,
            "MKT-A",
            now + 500 * MILLISECOND,
            3,
            epoch=1,
        ),
    ]
    records[1]["sid_gap_generation"] = 1
    records[2]["sid_gap_generation"] = 1
    decision = records[-1]["receive_timestamp_ns"]
    blocked = materialize_complete(
        books=records,
        metadata_rows=[metadata(date, "MKT-A", decision_ns=decision)],
        selections=[selection(date, "MKT-A", decision)],
        trades=[],
        exits=[],
        settlements=[],
    )
    assert (
        "BLOCK_A01_DECISION_BOOK_GAP_RESET_EPOCH"
        in blocker_codes(blocked)
    )
    assert blocked.rows == ()

    restored_ns = now + 750 * MILLISECOND
    restored_snapshot = book(
            date,
            "MKT-A",
            restored_ns,
            4,
            snapshot=True,
            epoch=2,
        )
    restored_snapshot["sid_gap_generation"] = 1
    restored_snapshot["market_revalidated_gap_generation"] = 1
    restored_records = records + [restored_snapshot]
    restored = materialize_complete(
        books=restored_records,
        metadata_rows=[
            metadata(date, "MKT-A", decision_ns=restored_ns)
        ],
        selections=[selection(date, "MKT-A", restored_ns)],
        trades=[],
        exits=[exit_request(date, "MKT-A", restored_ns)],
        settlements=[settlement("MKT-A")],
    )
    assert restored.ready
    assert restored.rows[0]["warmup_started_at_ns"] == restored_ns


def test_empty_trade_collection_requires_externally_pinned_coverage():
    date = "2026-07-17"
    now = utc_ns(date)
    artifact = threshold_artifact()
    result = materialize_a01(
        stage="VALIDATION",
        freeze_document=freeze(),
        training_artifact=artifact,
        expected_training_artifact_sha256=artifact.sha256,
        book_records=[book(date, "MKT-A", now, 1, snapshot=True)],
        metadata_records=[
            metadata(date, "MKT-A", decision_ns=now)
        ],
        opportunity_records=[
            opportunity(date, "ROOT-1", now)
        ],
        selection_records=[selection(date, "MKT-A", now)],
        public_trade_records=[],
        exit_requests=[exit_request(date, "MKT-A", now)],
        settlement_records=[settlement("MKT-A")],
    )
    assert "BLOCK_A01_SOURCE_COVERAGE_MISSING" in blocker_codes(result)
    assert not result.ready


def test_coverage_binds_collection_bytes_not_only_row_counts():
    date = "2026-07-17"
    now = utc_ns(date)
    books = [book(date, "MKT-A", now, 1, snapshot=True)]
    metadata_rows = [metadata(date, "MKT-A", decision_ns=now)]
    selections = [selection(date, "MKT-A", now)]
    exits: list[dict[str, Any]] = []
    settlements = [settlement("MKT-A")]
    coverage = source_coverage(
        stage="VALIDATION",
        books=books,
        metadata_rows=metadata_rows,
        selections=selections,
        trades=[],
        exits=exits,
        settlements=settlements,
    )
    mutated = [{**books[0], "source_sha256": H_B}]
    artifact = threshold_artifact()
    result = materialize_a01(
        stage="VALIDATION",
        freeze_document=freeze(),
        training_artifact=artifact,
        expected_training_artifact_sha256=artifact.sha256,
        source_coverage=coverage,
        expected_source_coverage_sha256=coverage.sha256,
        expected_opportunity_policy_sha256=H_D,
        expected_fee_facts_sha256=H_A,
        expected_measured_latency_receipt_sha256=H_B,
        expected_selection_policy_sha256=H_C,
        book_records=mutated,
        metadata_records=metadata_rows,
        opportunity_records=opportunities_for(selections),
        selection_records=selections,
        public_trade_records=[],
        exit_requests=exits,
        settlement_records=settlements,
    )
    assert (
        "BLOCK_A01_SOURCE_COVERAGE_CONTENT_MISMATCH"
        in blocker_codes(result)
    )
    assert not result.ready


def test_latest_terminal_revision_must_be_the_unique_final():
    date = "2026-07-17"
    now = utc_ns(date)
    final = settlement("MKT-A")
    later_void = {
        **final,
        "settlement_id": "settlement-MKT-A-revision-2",
        "status": "VOID",
        "finalized": False,
        "yes_settlement_value_e4": None,
        "observed_at_ns": final["observed_at_ns"] + SECOND,
        "revision": 2,
        "source_sha256": H_C,
    }
    result = materialize_complete(
        books=[book(date, "MKT-A", now, 1, snapshot=True)],
        metadata_rows=[metadata(date, "MKT-A", decision_ns=now)],
        selections=[selection(date, "MKT-A", now)],
        trades=[],
        exits=[exit_request(date, "MKT-A", now)],
        settlements=[final, later_void],
    )
    assert (
        "BLOCK_A01_SETTLEMENT_LATEST_NOT_UNIQUE_FINAL"
        in blocker_codes(result)
    )
    assert result.settlements == ()
    assert not result.ready


def test_exit_authority_source_is_bound_into_materialization_payload():
    date = "2026-07-17"
    now = utc_ns(date)
    decision_ns = now + 120 * SECOND
    common = {
        "books": continuous_books(date, "MKT-A", now),
        "metadata_rows": [
            metadata(date, "MKT-A", decision_ns=decision_ns)
        ],
        "selections": [selection(date, "MKT-A", decision_ns)],
        "trades": [],
        "settlements": [settlement("MKT-A")],
    }
    first_exit = exit_request(date, "MKT-A", decision_ns)
    second_exit = {**first_exit, "source_sha256": H_D}
    first = materialize_complete(exits=[first_exit], **common)
    second = materialize_complete(exits=[second_exit], **common)
    assert first.ready and second.ready
    assert (
        first.exit_snapshots[0]["snapshot_id"]
        == second.exit_snapshots[0]["snapshot_id"]
    )
    assert first.source_coverage_sha256 != second.source_coverage_sha256
    assert (
        first.to_dict()["payload_sha256"]
        != second.to_dict()["payload_sha256"]
    )


def test_each_triggered_intent_requires_its_own_post_cancel_exit():
    date = "2026-07-17"
    start = utc_ns(date)
    decision_times = [
        start + 120_000 * MILLISECOND,
        start + 120_250 * MILLISECOND,
    ]
    books = continuous_books(
        date,
        "MKT-A",
        start,
        duration_ms=126_000,
    )
    metadata_rows = [
        metadata(date, "MKT-A", decision_ns=decision_times[0])
    ]
    selections = [
        selection(date, "MKT-A", decision)
        for decision in decision_times
    ]
    artifact = threshold_artifact()
    bindings = RuntimeBindings(
        fee_facts_sha256=H_A,
        latency_receipt_sha256=H_B,
        root_map_sha256=H_C,
        scheduled_start_source_sha256=H_D,
        risk_policy_sha256="e" * 64,
        terminal_contract_sha256="f" * 64,
        strict_fill_evidence_sha256="0" * 64,
        card_parameter_artifact_sha256=artifact.sha256,
    )
    preflight = materialize_complete(
        books=books,
        metadata_rows=metadata_rows,
        selections=selections,
        trades=[],
        exits=[],
        settlements=[settlement("MKT-A")],
    )
    adapter = FrozenExperimentAdapters(freeze())
    explicit_exits: list[dict[str, Any]] = []
    for row in preflight.rows:
        decision = adapter.evaluate_a01(
            NormalizedStateRow(**row),
            bindings,
        )
        assert decision.status is DecisionStatus.ORDER_INTENTS
        for intent in decision.intents:
            exit_decision = (
                row["decision_ts_ns"]
                + intent.expire_after_ms * MILLISECOND
                + 100 * MILLISECOND
            )
            explicit_exits.append(
                {
                    "exit_id": f"exit|{intent.intent_id}",
                    "row_id": row["row_id"],
                    "intent_id": intent.intent_id,
                    "date": date,
                    "market_ticker": "MKT-A",
                    "exit_decision_ts_ns": exit_decision,
                    "effective_ts_ns": (
                        exit_decision + 100 * MILLISECOND
                    ),
                    "measured_ioc_exit_latency_ns": (
                        100 * MILLISECOND
                    ),
                    "exit_limit_price_e4": (
                        100
                        if intent.side.value == "BUY"
                        else 9_900
                    ),
                    "maximum_snapshot_age_us": 250_000,
                    "measured_latency_receipt_sha256": H_B,
                    "source_sha256": H_C,
                }
            )
    assert len(explicit_exits) == 4

    one_missing = materialize_complete(
        books=books,
        metadata_rows=metadata_rows,
        selections=selections,
        trades=[],
        exits=explicit_exits[:-1],
        settlements=[settlement("MKT-A")],
    )
    assert len(one_missing.closures) == 3
    assert (
        "BLOCK_A01_EXIT_DECISION_AUTHORITY_MISSING"
        in blocker_codes(one_missing)
    )
    assert not one_missing.ready

    before_cancel = {
        **explicit_exits[0],
        "exit_decision_ts_ns": (
            decision_times[0] + 5_000 * MILLISECOND
        ),
        "effective_ts_ns": (
            decision_times[0] + 5_100 * MILLISECOND
        ),
    }
    too_early = materialize_complete(
        books=books,
        metadata_rows=metadata_rows,
        selections=selections,
        trades=[],
        exits=[before_cancel, *explicit_exits[1:]],
        settlements=[settlement("MKT-A")],
    )
    assert (
        "BLOCK_A01_EXIT_SNAPSHOT_UNAVAILABLE"
        in blocker_codes(too_early)
    )
    assert not too_early.ready


def test_public_trade_fill_clock_is_local_receive_not_exchange_time():
    date = "2026-07-17"
    start = utc_ns(date)
    decision = start + 120 * SECOND
    raw_trade = trade(
        date,
        "MKT-A",
        decision + 2 * SECOND,
    )
    raw_trade["exchange_timestamp_us"] = (
        decision - 2 * SECOND
    ) // 1_000
    result = materialize_complete(
        books=continuous_books(date, "MKT-A", start),
        metadata_rows=[
            metadata(date, "MKT-A", decision_ns=decision)
        ],
        selections=[selection(date, "MKT-A", decision)],
        trades=[raw_trade],
        exits=[exit_request(date, "MKT-A", decision)],
        settlements=[settlement("MKT-A")],
    )
    assert result.ready
    assert result.public_trades[0]["timestamp_us"] == (
        raw_trade["receive_timestamp_us"]
    )
    assert result.public_trades[0]["timestamp_us"] != (
        raw_trade["exchange_timestamp_us"]
    )

    wrong_domain = {
        **raw_trade,
        "clock_domain": "EXCHANGE_EVENT",
    }
    blocked = materialize_complete(
        books=continuous_books(date, "MKT-A", start),
        metadata_rows=[
            metadata(date, "MKT-A", decision_ns=decision)
        ],
        selections=[selection(date, "MKT-A", decision)],
        trades=[wrong_domain],
        exits=[exit_request(date, "MKT-A", decision)],
        settlements=[settlement("MKT-A")],
    )
    assert "BLOCK_A01_TRADE_INVALID" in blocker_codes(blocked)
    assert not blocked.ready


def test_training_artifact_requires_external_pin():
    date = "2026-07-17"
    now = utc_ns(date)
    books = [book(date, "MKT-A", now, 1, snapshot=True)]
    metadata_rows = [metadata(date, "MKT-A", decision_ns=now)]
    selections = [selection(date, "MKT-A", now)]
    exits = [exit_request(date, "MKT-A", now)]
    settlements = [settlement("MKT-A")]
    coverage = source_coverage(
        stage="VALIDATION",
        books=books,
        metadata_rows=metadata_rows,
        selections=selections,
        trades=[],
        exits=exits,
        settlements=settlements,
    )
    result = materialize_a01(
        stage="VALIDATION",
        freeze_document=freeze(),
        training_artifact=threshold_artifact(),
        expected_training_artifact_sha256=None,
        source_coverage=coverage,
        expected_source_coverage_sha256=coverage.sha256,
        expected_opportunity_policy_sha256=H_D,
        expected_fee_facts_sha256=H_A,
        expected_measured_latency_receipt_sha256=H_B,
        expected_selection_policy_sha256=H_C,
        book_records=books,
        metadata_records=metadata_rows,
        opportunity_records=opportunities_for(selections),
        selection_records=selections,
        public_trade_records=[],
        exit_requests=exits,
        settlement_records=settlements,
    )
    assert (
        "BLOCK_A01_TRAIN_ARTIFACT_EXTERNAL_PIN_MISSING"
        in blocker_codes(result)
    )
    assert not result.ready


def test_future_candidate_metadata_cannot_choose_validation_market():
    date = "2026-07-17"
    now = utc_ns(date)
    meta = metadata(date, "MKT-A", decision_ns=now)
    meta["lifecycle_asof_ns"] = now + 1
    result = materialize_complete(
        books=[book(date, "MKT-A", now, 1, snapshot=True)],
        metadata_rows=[meta],
        selections=[selection(date, "MKT-A", now)],
        trades=[],
        exits=[],
        settlements=[],
    )
    assert "BLOCK_A01_SELECTION_INVALID" in blocker_codes(result)
    assert result.rows == ()


def test_monotonic_clock_regression_breaks_continuity():
    date = "2026-07-17"
    now = utc_ns(date)
    first = book(date, "MKT-A", now, 1, snapshot=True)
    regressed = book(
        date,
        "MKT-A",
        now + 250 * MILLISECOND,
        2,
    )
    regressed["receive_monotonic_ns"] = (
        first["receive_monotonic_ns"] - 1
    )
    decision = regressed["receive_timestamp_ns"]
    result = materialize_complete(
        books=[first, regressed],
        metadata_rows=[metadata(date, "MKT-A", decision_ns=decision)],
        selections=[selection(date, "MKT-A", decision)],
        trades=[],
        exits=[],
        settlements=[],
    )
    assert (
        "BLOCK_A01_DECISION_BOOK_GAP_RESET_EPOCH"
        in blocker_codes(result)
    )
    assert result.rows == ()


def test_monotonic_blind_gap_cannot_be_hidden_by_wall_clock():
    date = "2026-07-17"
    now = utc_ns(date)
    records = [
        book(
            date,
            "MKT-A",
            now + index * 250 * MILLISECOND,
            index + 1,
            snapshot=index == 0,
        )
        for index in range(3)
    ]
    for index, row in enumerate(records):
        row["receive_monotonic_ns"] = now + index * SECOND
    decision = records[-1]["receive_timestamp_ns"]
    result = materialize_complete(
        books=records,
        metadata_rows=[metadata(date, "MKT-A", decision_ns=decision)],
        selections=[selection(date, "MKT-A", decision)],
        trades=[],
        exits=[],
        settlements=[],
    )
    assert (
        "BLOCK_A01_RECEIVE_CLOCK_DIVERGENCE"
        in blocker_codes(result)
    )
    assert result.rows == ()


def test_full_depth_and_replay_authority_are_mandatory():
    date = "2026-07-17"
    now = utc_ns(date)
    incomplete = book(date, "MKT-A", now, 1, snapshot=True)
    incomplete["depth_complete"] = False
    parsed_block = materialize_complete(
        books=[incomplete],
        metadata_rows=[metadata(date, "MKT-A", decision_ns=now)],
        selections=[selection(date, "MKT-A", now)],
        trades=[],
        exits=[],
        settlements=[],
    )
    assert "BLOCK_A01_L2_RECORD_INVALID" in blocker_codes(parsed_block)

    wrong_receipt = book(date, "MKT-A", now, 1, snapshot=True)
    wrong_receipt["replay_receipt_sha256"] = H_B
    authority_block = materialize_complete(
        books=[wrong_receipt],
        metadata_rows=[metadata(date, "MKT-A", decision_ns=now)],
        selections=[selection(date, "MKT-A", now)],
        trades=[],
        exits=[exit_request(date, "MKT-A", now)],
        settlements=[settlement("MKT-A")],
    )
    assert (
        "BLOCK_A01_FULL_DEPTH_REPLAY_RECEIPT_MISMATCH"
        in blocker_codes(authority_block)
    )
    assert not authority_block.ready


def test_public_trade_price_must_follow_direct_tick_table():
    date = "2026-07-17"
    now = utc_ns(date)
    off_tick = trade(date, "MKT-A", now)
    off_tick["yes_price_e4"] = 3_950
    result = materialize_complete(
        books=[book(date, "MKT-A", now, 1, snapshot=True)],
        metadata_rows=[metadata(date, "MKT-A", decision_ns=now)],
        selections=[selection(date, "MKT-A", now)],
        trades=[off_tick],
        exits=[exit_request(date, "MKT-A", now)],
        settlements=[settlement("MKT-A")],
    )
    assert "BLOCK_A01_TRADE_INVALID" in blocker_codes(result)
    assert result.public_trades == ()
    assert not result.ready


def test_cumulative_wall_monotonic_drift_cannot_earn_warmup():
    date = "2026-07-17"
    now = utc_ns(date)
    decision = now + 120 * SECOND
    records = continuous_books(
        date,
        "MKT-A",
        now,
        duration_ms=120_000,
    )
    for index, row in enumerate(records):
        row["receive_monotonic_ns"] = now + index
    result = materialize_complete(
        books=records,
        metadata_rows=[
            metadata(date, "MKT-A", decision_ns=decision)
        ],
        selections=[selection(date, "MKT-A", decision)],
        trades=[],
        exits=[exit_request(date, "MKT-A", decision)],
        settlements=[settlement("MKT-A")],
    )
    assert (
        "BLOCK_A01_RECEIVE_CLOCK_DIVERGENCE"
        in blocker_codes(result)
    )
    assert not result.ready


def test_sid_gap_invalidates_every_market_until_its_own_snapshot():
    date = "2026-07-17"
    now = utc_ns(date)
    decision = now + 120 * SECOND
    market_a = continuous_books(
        date,
        "MKT-A",
        now,
        duration_ms=119_750,
        sid=7,
    )
    gap_on_b = book(
        date,
        "MKT-B",
        decision - 100 * MILLISECOND,
        481,
        sid=7,
        gap=True,
    )
    gap_on_b["sid_gap_generation"] = 1
    stale_a_delta = book(
        date,
        "MKT-A",
        decision,
        482,
        sid=7,
    )
    stale_a_delta["sid_gap_generation"] = 1
    stale_a_delta["market_revalidated_gap_generation"] = 0
    result = materialize_complete(
        books=[*market_a, gap_on_b, stale_a_delta],
        metadata_rows=[
            metadata(date, "MKT-A", decision_ns=decision),
            metadata(date, "MKT-B", decision_ns=decision),
        ],
        selections=[selection(date, "MKT-A", decision)],
        trades=[],
        exits=[exit_request(date, "MKT-A", decision)],
        settlements=[settlement("MKT-A")],
    )
    assert (
        "BLOCK_A01_DECISION_BOOK_GAP_RESET_EPOCH"
        in blocker_codes(result)
        or "BLOCK_A01_SID_SEQUENCE_INVALID"
        in blocker_codes(result)
    )
    assert not result.ready


def test_cli_authority_is_raw_pinned_and_output_is_create_once(tmp_path):
    date = "2026-07-17"
    now = utc_ns(date)
    books = [book(date, "MKT-A", now, 1, snapshot=True)]
    metadata_rows = [metadata(date, "MKT-A", decision_ns=now)]
    selections = [selection(date, "MKT-A", now)]
    trades: list[dict[str, Any]] = []
    exits: list[dict[str, Any]] = []
    settlements = [settlement("MKT-A")]
    artifact = threshold_artifact()
    coverage = source_coverage(
        stage="VALIDATION",
        books=books,
        metadata_rows=metadata_rows,
        selections=selections,
        trades=trades,
        exits=exits,
        settlements=settlements,
    )
    payloads = {
        "freeze_document": freeze(),
        "training_artifact": artifact.to_dict(),
        "source_coverage": coverage.to_dict(),
        "book_records": books,
        "metadata_records": metadata_rows,
        "opportunity_records": opportunities_for(selections),
        "selection_records": selections,
        "public_trade_records": trades,
        "exit_requests": exits,
        "settlement_records": settlements,
    }
    inputs: dict[str, dict[str, str]] = {}
    for name, payload in payloads.items():
        path = tmp_path / f"{name}.json"
        # The raw file pin is deliberately distinct from canonical payload
        # hashing; whitespace is covered too.
        file_bytes = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        path.write_bytes(file_bytes)
        inputs[name] = {
            "path": str(path),
            "raw_sha256": hashlib.sha256(file_bytes).hexdigest(),
            "format": a01_materialize_cli.INPUT_LAYOUT[name],
        }
    output = tmp_path / "A01_MATERIALIZATION_RECEIPT.json"
    code_dir = Path(a01_materialize_cli.__file__).parent
    code_files = {
        filename: {
            "path": str(code_dir / filename),
            "raw_sha256": hashlib.sha256(
                (code_dir / filename).read_bytes()
            ).hexdigest(),
        }
        for filename in a01_materialize_cli.CODE_DEPENDENCY_CLOSURE
    }
    authority = {
        "schema_version": a01_materialize_cli.AUTHORITY_SCHEMA,
        "authority_id": "A01-TEST-AUTHORITY-01",
        "stage": "VALIDATION",
        "code_files": code_files,
        "expected_training_artifact_sha256": artifact.sha256,
        "expected_source_coverage_sha256": coverage.sha256,
        "expected_opportunity_policy_sha256": H_D,
        "expected_fee_facts_sha256": H_A,
        "expected_measured_latency_receipt_sha256": H_B,
        "expected_selection_policy_sha256": H_C,
        "inputs": inputs,
        "output_path": str(output),
    }
    receipt = a01_materialize_cli.execute_authority(
        authority,
        authority_raw_sha256=H_A,
        require_root_read_only_code=False,
    )
    assert receipt["state"] == "BLOCKED"
    assert receipt["fixture_fragment"] is None
    assert {
        row["code"] for row in receipt["blockers"]
    } == {
        row["code"]
        for row in a01_materialize_cli.UNRESOLVED_PRODUCTION_GATES
    }
    assert (
        "BLOCK_A01_PATHWISE_FIRST_FILL_CANCEL_ENGINE_MISSING"
        not in {
            row["code"]
            for row in a01_materialize_cli.UNRESOLVED_PRODUCTION_GATES
        }
    )
    assert {
        row["code"]
        for row in a01_materialize_cli.UNRESOLVED_PRODUCTION_GATES
    } == {
        "BLOCK_A01_LATENCY_FEE_DERIVATION_NOT_BOUND",
        "BLOCK_A01_POINT_IN_TIME_METADATA_INTERVALS_MISSING",
        "BLOCK_A01_TRADE_CLOCK_EPOCH_AND_NS_ENGINE_MISSING",
        "BLOCK_A01_OPPORTUNITY_DENOMINATOR_LEDGER_MISSING",
    }
    assert receipt["materialization"]["rows"]

    first_sha = a01_materialize_cli.write_receipt_create_once(
        output,
        receipt,
    )
    first_bytes = output.read_bytes()
    assert len(first_sha) == 64
    with pytest.raises(Exception, match="create-once refuses overwrite"):
        a01_materialize_cli.write_receipt_create_once(
            output,
            receipt,
        )
    assert output.read_bytes() == first_bytes

    bad_authority = {
        **authority,
        "authority_id": "A01-TEST-AUTHORITY-BAD-PIN",
        "inputs": {
            **inputs,
            "book_records": {
                **inputs["book_records"],
                "raw_sha256": "0" * 64,
            },
        },
        "output_path": str(tmp_path / "blocked.json"),
    }
    blocked = a01_materialize_cli.execute_authority(
        bad_authority,
        authority_raw_sha256=H_B,
        require_root_read_only_code=False,
    )
    assert blocked["state"] == "BLOCKED"
    assert {
        row["code"] for row in blocked["blockers"]
    } == {"BLOCK_A01_RAW_SHA_MISMATCH"}

    input_reads = 0
    original_reader = a01_materialize_cli._read_pinned_json

    def counted_reader(*args: Any, **kwargs: Any):
        nonlocal input_reads
        input_reads += 1
        return original_reader(*args, **kwargs)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        a01_materialize_cli,
        "_read_pinned_json",
        counted_reader,
    )
    bad_code_files = {
        name: dict(descriptor)
        for name, descriptor in code_files.items()
    }
    bad_code_files["contracts.py"]["raw_sha256"] = "0" * 64
    try:
        code_blocked = a01_materialize_cli.execute_authority(
            {**authority, "code_files": bad_code_files},
            authority_raw_sha256=H_C,
            require_root_read_only_code=False,
        )
    finally:
        monkeypatch.undo()
    assert code_blocked["state"] == "BLOCKED"
    assert {
        row["code"] for row in code_blocked["blockers"]
    } == {"BLOCK_A01_CODE_PIN_MISMATCH"}
    assert input_reads == 0


def test_future_ineligible_market_cannot_shrink_candidate_universe():
    date = "2026-07-17"
    now = utc_ns(date)
    future_paused = metadata(
        date,
        "MKT-B",
        root="ROOT-2",
        decision_ns=now,
    )
    future_paused["paused"] = True
    future_paused["lifecycle_asof_ns"] = now + 1
    result = materialize_complete(
        books=[
            book(date, "MKT-A", now, 1, snapshot=True),
            book(date, "MKT-B", now, 1, snapshot=True),
        ],
        metadata_rows=[
            metadata(date, "MKT-A", decision_ns=now),
            future_paused,
        ],
        selections=[selection(date, "MKT-A", now)],
        trades=[],
        exits=[exit_request(date, "MKT-A", now)],
        settlements=[settlement("MKT-A")],
        opportunities=[
            opportunity(date, "ROOT-1", now, suffix="root-1"),
            opportunity(date, "ROOT-2", now, suffix="root-2"),
        ],
    )
    assert (
        "BLOCK_A01_OPPORTUNITY_INVALID"
        in blocker_codes(result)
    )
    assert not result.ready


def test_fee_latency_and_selection_policy_bind_every_decision_and_exit():
    date = "2026-07-17"
    now = utc_ns(date)
    bad_selection = selection(date, "MKT-A", now)
    bad_selection["fee_facts_sha256"] = H_D
    bad_exit = exit_request(date, "MKT-A", now)
    bad_exit["measured_latency_receipt_sha256"] = H_D
    result = materialize_complete(
        books=[book(date, "MKT-A", now, 1, snapshot=True)],
        metadata_rows=[metadata(date, "MKT-A", decision_ns=now)],
        selections=[bad_selection],
        trades=[],
        exits=[bad_exit],
        settlements=[settlement("MKT-A")],
    )
    assert "BLOCK_A01_SELECTION_INVALID" in blocker_codes(result)
    assert result.rows == ()
    assert not result.ready
