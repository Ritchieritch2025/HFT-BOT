"""Pure-synthetic contract tests for the ROUND4 table-builder scaffold."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from tools.research.crypto_mm.round4_table_builder import (
    ActionSetPendingError,
    CANDIDATE_STATUS,
    EXPERIMENT_ID,
    ForbiddenSourceError,
    ReceiptBook,
    Round4ContractError,
    Round4TableBuilder,
    create_round4_schema,
    merge_receipt_events,
    read_preflighted_sources,
    reconstruct_receipt_stream,
)


T0 = 1_000_000_000_000
ACTIVE = T0 + 10_000_000
FILL = ACTIVE + 1_500_000_000
LINEAGE = "a" * 64


def book_event(
    kind,
    *,
    wall,
    seq,
    sid=7,
    stable_id=None,
    **extra,
):
    row = {
        "channel": "BOOK",
        "kind": kind,
        "market_ticker": "SYNTH-MARKET",
        "stable_source_id": stable_id or f"book-{sid}-{seq}",
        "recv_wall_ns": wall,
        "recv_mono_ns": wall // 10,
        "local_recv_ts_us": wall // 1_000,
        "ws_sid": sid,
        "ws_seq": seq,
    }
    row.update(extra)
    return row


def snapshot_event(*, wall=T0, seq=10, sid=7, stable_id="snapshot"):
    return book_event(
        "snapshot",
        wall=wall,
        seq=seq,
        sid=sid,
        stable_id=stable_id,
        yes_levels=[(3_000, Decimal("2"))],
        no_levels=[(6_900, Decimal("3"))],
    )


def delta_event(
    *,
    wall=T0 + 1_000,
    seq=11,
    sid=7,
    side="yes",
    delta="-2",
    stable_id="delta",
):
    return book_event(
        "delta",
        wall=wall,
        seq=seq,
        sid=sid,
        stable_id=stable_id,
        side=side,
        price_e4=3_000,
        delta_fp=delta,
    )


def trade_event(*, wall=T0, stable_id="trade"):
    return {
        "channel": "TRADE",
        "kind": "trade",
        "market_ticker": "SYNTH-MARKET",
        "stable_source_id": stable_id,
        "recv_wall_ns": wall,
        "recv_mono_ns": wall // 10,
        "local_recv_ts_us": wall // 1_000,
    }


def entry_episode(
    *,
    episode_id="episode-1",
    action_id="synthetic-pair",
    cycle_id="cycle-1",
    active=ACTIVE,
    terminal=FILL,
    cause="YES_FIRST",
):
    return {
        "experiment_id": EXPERIMENT_ID,
        "data_role": "DISCOVERY",
        "source_date_utc": "2026-07-20",
        "market_ticker": "SYNTH-MARKET",
        "market_cluster_id": "SYNTH-MARKET",
        "cycle_id": cycle_id,
        "entry_episode_id": episode_id,
        "entry_action_id": action_id,
        "action_set_version": "PENDING_SYNTHETIC_V0",
        "source_rows_sha256": LINEAGE,
        "decision_recv_wall_ns": T0,
        "decision_recv_mono_ns": T0 // 10,
        "feature_asof_wall_ns": T0,
        "entry_active_wall_ns": active,
        "terminal_wall_ns": terminal,
        "book_ws_sid": "7",
        "book_ws_seq": 10,
        "action_kind": "ENTRY_PAIR_POST_ONLY",
        "yes_price_e4": 3_000,
        "no_price_e4": 6_900,
        "clip_fp": Decimal("1"),
        "pair_cost_e4": 9_900,
        "locked_pair_ceiling_e4": 9_900,
        "legal_grid": True,
        "yes_post_only": True,
        "no_post_only": True,
        "ack_state": "BOTH_ACKED",
        "tte_ms": 600_000,
        "yes_same_price_ahead_fp": Decimal("2"),
        "no_same_price_ahead_fp": Decimal("3"),
        "yes_better_depth_fp": Decimal("0"),
        "no_better_depth_fp": Decimal("0"),
        "yes_flow_10s_fp": Decimal("1"),
        "no_flow_10s_fp": Decimal("-1"),
        "yes_flow_60s_fp": Decimal("5"),
        "no_flow_60s_fp": Decimal("-5"),
        "touch_imbalance": Decimal("0.1"),
        "spread_e4": 100,
        "mid_move_1s_e4": 0,
        "mid_move_10s_e4": 10,
        "terminal_cause": cause,
        "first_fill_wall_ns": (
            terminal if cause in ("YES_FIRST", "NO_FIRST") else None
        ),
        "censor_reason": None,
    }


def first_fill_state(
    *,
    side="YES",
    postfill_id="postfill-1",
    complement_remaining=Decimal("1"),
):
    return {
        "postfill_episode_id": postfill_id,
        "entry_episode_id": "episode-1",
        "entry_action_id": "synthetic-pair",
        "source_rows_sha256": LINEAGE,
        "first_fill_side": side,
        "first_fill_price_e4": 3_000,
        "first_fill_qty_fp": Decimal("1"),
        "first_fill_fee_usd": Decimal("0"),
        "first_fill_recv_wall_ns": FILL,
        "first_fill_recv_mono_ns": FILL // 10,
        "feature_asof_wall_ns": FILL,
        "first_fill_elapsed_ms": Decimal("1500"),
        "complement_order_id": "no-order",
        "complement_side": "NO" if side == "YES" else "YES",
        "complement_price_e4": 6_900,
        "complement_remaining_qty_fp": complement_remaining,
        "complement_order_age_ms": 1_500,
        "complement_queue_position_fp": Decimal("3"),
        "complement_same_price_ahead_fp": Decimal("3"),
        "complement_better_depth_fp": Decimal("0"),
        "complement_flow_1s_fp": Decimal("0"),
        "complement_flow_5s_fp": Decimal("0"),
        "complement_flow_10s_fp": Decimal("-1"),
        "complement_flow_60s_fp": Decimal("-5"),
        "spread_e4": 100,
        "mid_move_since_entry_e4": 10,
        "mid_move_since_fill_e4": 0,
        "pair_gain_if_complement_usd": Decimal("0.01"),
        "buy_complement_exit_pnl_usd": Decimal("-0.01"),
        "sell_first_exit_pnl_usd": Decimal("-0.02"),
        "buy_complement_executable_qty_fp": Decimal("1"),
        "sell_first_executable_qty_fp": Decimal("1"),
        "tte_ms": 598_500,
        "cancel_state": "NONE",
    }


def valid_builder():
    builder = Round4TableBuilder()
    key = builder.add_entry_episode(entry_episode())
    builder.materialize_entry_risk_intervals(key)
    builder.add_first_fill_state(first_fill_state())
    return builder


def zero_time_atom():
    return {
        "postfill_episode_id": "postfill-1",
        "entry_episode_id": "episode-1",
        "entry_action_id": "synthetic-pair",
        "source_rows_sha256": LINEAGE,
        "first_fill_side": "YES",
        "complement_side": "NO",
        "atom_recv_wall_ns": FILL,
        "atom_recv_mono_ns": FILL // 10,
        "receipt_envelope_id": "envelope-1",
        "first_fill_stable_source_id": "trade-001",
        "complement_fill_stable_source_id": "trade-002",
        "complement_fill_price_e4": 6_900,
        "complement_fill_qty_fp": Decimal("1"),
        "complement_fill_fee_usd": Decimal("0"),
        "reconciliation_ok": True,
    }


@pytest.mark.parametrize(
    "forbidden_path",
    (
        "/synthetic/date=2026-07-23/book.parquet",
        "/synthetic/date=20260723/book.parquet",
        "/synthetic/date=2026-07-26/book.parquet",
        "/synthetic/date=20260726/book.parquet",
    ),
)
def test_forbidden_path_batch_fails_before_any_reader_call(
    forbidden_path,
):
    calls = []

    def reader(path):
        calls.append(path)
        return b"must-not-run"

    with pytest.raises(ForbiddenSourceError, match="forbidden-date"):
        read_preflighted_sources(
            (
                "/synthetic/date=2026-07-20/book.parquet",
                forbidden_path,
            ),
            "2026-07-20",
            reader=reader,
        )
    assert calls == []


def test_only_positive_discovery_paths_reach_reader():
    calls = []

    def reader(path):
        calls.append(path)
        return f"read:{path.name}"

    result = read_preflighted_sources(
        (
            "/synthetic/date=2026-07-20/book.parquet",
            "/synthetic/date=2026-07-20/trade.parquet",
        ),
        "2026-07-20",
        reader=reader,
    )
    assert result == ("read:book.parquet", "read:trade.parquet")
    assert len(calls) == 2
    with pytest.raises(ActionSetPendingError):
        read_preflighted_sources(
            ("/synthetic/date=2026-07-24/book.parquet",),
            "2026-07-24",
            reader=reader,
            data_role="FORWARD",
        )


@pytest.mark.parametrize(
    "extra_token",
    (
        "2026-07-19",
        "20260719",
        "2026_07_19",
        "2026/07/19",
        "2025-12-31",
    ),
)
def test_any_additional_date_token_blocks_entire_batch(extra_token):
    calls = []
    with pytest.raises(ForbiddenSourceError, match="additional date"):
        read_preflighted_sources(
            (
                "/synthetic/date=2026-07-20/book.parquet",
                (
                    "/synthetic/date=2026-07-20/"
                    f"archive={extra_token}/trade.parquet"
                ),
            ),
            "2026-07-20",
            reader=lambda path: calls.append(path),
        )
    assert calls == []


def test_receipt_merge_is_book_before_trade_in_same_envelope():
    merged = merge_receipt_events((trade_event(), snapshot_event()))
    assert [row["channel"] for row in merged] == ["BOOK", "TRADE"]
    merged, states, timeline = reconstruct_receipt_stream(
        (trade_event(), snapshot_event())
    )
    assert len(merged) == 2
    assert timeline[-1]["book"]["book_ws_seq"] == 10
    assert states["SYNTH-MARKET"].books["yes"][3_000] == 2


def test_receipt_clock_identity_is_mandatory():
    bad = snapshot_event()
    bad["local_recv_ts_us"] += 1
    with pytest.raises(Round4ContractError, match="identity"):
        merge_receipt_events((bad,))


def test_signed_delta_zero_deletes_without_clamp():
    book = ReceiptBook("SYNTH-MARKET")
    book.apply(snapshot_event())
    book.apply(delta_event())
    assert 3_000 not in book.books["yes"]
    assert book.zero_delete_count == 1


@pytest.mark.parametrize(
    "events,match",
    (
        ((delta_event(),), "snapshot"),
        (
            (
                snapshot_event(),
                delta_event(sid=8),
            ),
            "same-sid",
        ),
        (
            (
                snapshot_event(),
                delta_event(seq=10),
            ),
            "sequence",
        ),
        (
            (
                snapshot_event(),
                delta_event(delta="-3"),
            ),
            "negative signed depth",
        ),
    ),
)
def test_snapshot_sid_seq_signed_depth_fail_closed(events, match):
    with pytest.raises(Round4ContractError, match=match):
        reconstruct_receipt_stream(events)


def test_entry_intervals_and_first_fill_conserve_exactly():
    builder = valid_builder()
    receipt = builder.validate_core()
    rows = list(builder.entry_risk_intervals.values())
    assert [(row["elapsed_start_ms"], row["elapsed_stop_ms"]) for row in rows] == [
        (Decimal("0"), Decimal("1000")),
        (Decimal("1000"), Decimal("1500")),
    ]
    assert sum(row["event_yes_first"] for row in rows) == 1
    assert sum(row["event_no_first"] for row in rows) == 0
    assert receipt["entry_episode_rows"] == 1
    assert receipt["entry_risk_interval_rows"] == 2
    assert receipt["first_fill_state_rows"] == 1


def test_duplicate_primary_keys_and_overlapping_cycles_are_rejected():
    builder = Round4TableBuilder()
    first = entry_episode()
    builder.add_entry_episode(first)
    with pytest.raises(Round4ContractError, match="duplicate"):
        builder.add_entry_episode(first)
    overlapping = entry_episode(
        episode_id="episode-2",
        action_id="synthetic-pair-2",
        cycle_id="cycle-2",
        active=ACTIVE + 1,
        terminal=FILL + 1,
    )
    with pytest.raises(Round4ContractError, match="overlapping"):
        builder.add_entry_episode(overlapping)


def test_missing_or_wrong_first_fill_state_fails_conservation():
    builder = Round4TableBuilder()
    key = builder.add_entry_episode(entry_episode())
    builder.materialize_entry_risk_intervals(key)
    with pytest.raises(Round4ContractError, match="existence"):
        builder.validate_core()
    with pytest.raises(Round4ContractError, match="side/cause"):
        builder.add_first_fill_state(first_fill_state(side="NO"))


def test_data_invalid_never_enters_analytical_tables():
    builder = Round4TableBuilder()
    invalid = entry_episode(cause="DATA_INVALID")
    invalid["first_fill_wall_ns"] = None
    with pytest.raises(Round4ContractError, match="entire market-day"):
        builder.add_entry_episode(invalid)


def test_sealed_ddl_rejects_data_invalid_risk_row():
    connection = duckdb.connect(":memory:")
    create_round4_schema(connection)
    with pytest.raises(duckdb.ConstraintException):
        connection.execute(
            """
            INSERT INTO entry_risk_interval (
                entry_episode_id, entry_action_id, interval_index,
                interval_start_wall_ns, interval_stop_wall_ns,
                feature_asof_wall_ns, elapsed_start_ms, elapsed_stop_ms,
                at_risk, event_yes_first, event_no_first,
                admin_censor, data_invalid
            ) VALUES (
                'invalid', 'action', 0,
                1000000000, 2000000000,
                1000000000, 0, 1000,
                TRUE, 0, 0, 0, 1
            )
            """
        )


def test_same_envelope_completion_is_explicit_zero_time_atom():
    builder = Round4TableBuilder()
    key = builder.add_entry_episode(entry_episode())
    builder.materialize_entry_risk_intervals(key)
    builder.add_first_fill_state(
        first_fill_state(complement_remaining=Decimal("0"))
    )
    builder.add_zero_time_atom(zero_time_atom())
    receipt = builder.validate_core()
    assert receipt["postfill_zero_time_atom_rows"] == 1
    connection = duckdb.connect(":memory:")
    create_round4_schema(connection)
    builder.insert_core_tables(connection)
    atom = connection.execute(
        "SELECT atom_recv_wall_ns, complement_fill_qty_fp "
        "FROM postfill_zero_time_atom"
    ).fetchone()
    assert atom[0] == FILL
    assert atom[1] == Decimal("1.00000000")
    columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info('postfill_zero_time_atom')"
        ).fetchall()
    }
    assert "elapsed_start_ms" not in columns
    assert "elapsed_stop_ms" not in columns


def test_postfill_interface_is_pending_and_write_blocked():
    builder = Round4TableBuilder()
    interface = builder.postfill_interface()
    assert interface == {
        "candidate_status": CANDIDATE_STATUS,
        "tables": (
            "postfill_decision",
            "postfill_action",
            "postfill_risk_interval",
            "postfill_action_outcome",
        ),
        "writes_enabled": False,
        "fit_enabled": False,
        "candidate_selection_enabled": False,
    }
    with pytest.raises(ActionSetPendingError):
        builder.add_postfill_row("postfill_action", {})


def test_sealed_ddl_accepts_valid_synthetic_core_rows():
    connection = duckdb.connect(":memory:")
    ddl_path = create_round4_schema(connection)
    assert isinstance(ddl_path, Path)
    receipt = valid_builder().insert_core_tables(connection)
    counts = {
        table: connection.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()[0]
        for table in (
            "entry_episode",
            "entry_risk_interval",
            "first_fill_state",
        )
    }
    assert counts == {
        "entry_episode": 1,
        "entry_risk_interval": 2,
        "first_fill_state": 1,
    }
    assert receipt["status"] == "ROUND4_CORE_SCAFFOLD_VALID"
