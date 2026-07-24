"""Synthetic adversarial tests for the isolated H4 event-study runner."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Dict, Iterable, List

import duckdb
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.research.alpha_sprint.h4_orderflow_event_study import (  # noqa: E402
    ALL_DATES,
    H4StudyError,
    MODEL_SEAL_NAME,
    RECEIPT_NAME,
    REPORT_NAME,
    run_event_study,
)


SOURCE_BINDING = "a" * 64


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


L2_SCHEMA = """
date VARCHAR,t_us BIGINT,recv_wall_ns BIGINT,recv_mono_ns BIGINT,
market_ticker VARCHAR,sport VARCHAR,ws_sid BIGINT,ws_seq BIGINT,
classification VARCHAR,snapshot_epoch BIGINT,
book_valid BOOLEAN,topology VARCHAR,bid_e4 BIGINT,bid_qty_e4 BIGINT,
ask_e4 BIGINT,ask_qty_e4 BIGINT,mid_e4 DOUBLE,microprice_e4 DOUBLE,
imbalance_depth3 DOUBLE,top_changed BOOLEAN
"""
TRADE_SCHEMA = """
date VARCHAR,t_us BIGINT,market_ticker VARCHAR,count_e4 BIGINT,
taker_side VARCHAR
"""


def _write_partition(
    con: Any,
    namespace: Path,
    stage: str,
    key: str,
    schema: str,
    rows: Iterable[Iterable[Any]],
) -> Dict[str, Any]:
    data_dir = namespace / stage / "data"
    receipt_dir = namespace / stage / "receipts"
    data_dir.mkdir(parents=True, exist_ok=True)
    receipt_dir.mkdir(parents=True, exist_ok=True)
    table = f"build_{stage}"
    con.execute(f"DROP TABLE IF EXISTS {table}")
    con.execute(f"CREATE TABLE {table}({schema})")
    rows = list(rows)
    if rows:
        width = len(rows[0])
        con.executemany(
            f"INSERT INTO {table} VALUES ({','.join('?' for _ in range(width))})",
            rows,
        )
    data_path = data_dir / f"{key}.parquet"
    con.execute(
        f"COPY {table} TO ? (FORMAT PARQUET,COMPRESSION ZSTD)",
        [str(data_path)],
    )
    described = con.execute(
        "DESCRIBE SELECT * FROM read_parquet(?)", [str(data_path)]
    ).fetchall()
    schema_receipt = [{"name": row[0], "type": row[1]} for row in described]
    row_count = int(
        con.execute("SELECT count(*) FROM read_parquet(?)", [str(data_path)])
        .fetchone()[0]
    )
    receipt = {
        "schema_version": "deep03-bounded-checkpoint-v1",
        "state": "COMPLETE",
        "stage": stage,
        "stage_version": f"{stage}-synthetic-v1",
        "partition_key": key,
        "source_binding": SOURCE_BINDING,
        "data": {
            "path": data_path.relative_to(namespace).as_posix(),
            "sha256": _sha256(data_path),
            "size_bytes": data_path.stat().st_size,
            "row_count": row_count,
            "schema": schema_receipt,
        },
        "metrics": {},
    }
    receipt_path = receipt_dir / f"{key}.json"
    receipt_path.write_bytes(_canonical_bytes(receipt))
    return {
        "partition_key": key,
        "receipt_path": receipt_path.relative_to(namespace).as_posix(),
        "receipt_sha256": _sha256(receipt_path),
        "data_sha256": receipt["data"]["sha256"],
        "row_count": row_count,
    }


def _l2_row(
    date: str,
    ticker: str,
    sport: str,
    t_us: int,
    sequence: int,
    bid: int,
    ask: int,
    *,
    bid_qty: int = 20_000,
    ask_qty: int = 20_000,
    microprice: float = 5_000.0,
    imbalance: float = 0.0,
    top_changed: bool = False,
    classification: str = "DELTA_APPLIED",
    snapshot_epoch: int = 1,
    topology: Any = "TWO_SIDED",
) -> List[Any]:
    return [
        date,
        t_us,
        t_us * 1_000,
        t_us * 1_000,
        ticker,
        sport,
        1,
        sequence,
        classification,
        snapshot_epoch,
        True,
        topology,
        bid,
        bid_qty,
        ask,
        ask_qty,
        (bid + ask) / 2.0,
        microprice,
        imbalance,
        top_changed,
    ]


def _market_rows(
    date: str,
    *,
    ticker: str = "KXTEST-TENNIS",
    sport: str = "Tennis",
    entry_depth: bool = True,
    entry_depth_null: bool = False,
    outcome_depth_null: bool = False,
    outcome_topology_null: bool = False,
    validation_flip: bool = False,
    reconnect: bool = False,
    stale_5s_outcome: bool = False,
) -> List[List[Any]]:
    quantity = None if entry_depth_null else (20_000 if entry_depth else 0)
    current_microprice = 4_950.0 if validation_flip else 5_050.0
    imbalance = -0.8 if validation_flip else 0.8
    decision_epoch = 2 if reconnect else 1
    rows = [
        _l2_row(
            date,
            ticker,
            sport,
            9_000_000,
            1,
            4_500,
            5_500,
            classification="SNAPSHOT_APPLIED",
        ),
    ]
    if reconnect:
        # Reconnect snapshots are top changes but are anchors, not signals.
        rows.append(
            _l2_row(
                date,
                ticker,
                sport,
                9_900_000,
                2,
                4_700,
                5_300,
                microprice=5_000.0,
                top_changed=True,
                classification="SNAPSHOT_APPLIED",
                snapshot_epoch=2,
            )
        )
    rows.extend(
        [
        _l2_row(
            date,
            ticker,
            sport,
            10_000_000,
            2,
            4_900,
            5_100,
            bid_qty=quantity,
            ask_qty=quantity,
            microprice=current_microprice,
            imbalance=imbalance,
            top_changed=True,
            snapshot_epoch=decision_epoch,
        ),
        _l2_row(
            date,
            ticker,
            sport,
            10_100_000,
            3,
            5_200,
            5_300,
            bid_qty=None if outcome_depth_null else 20_000,
            snapshot_epoch=decision_epoch,
            topology=None if outcome_topology_null else "TWO_SIDED",
        ),
        _l2_row(
            date,
            ticker,
            sport,
            11_000_000,
            4,
            5_400,
            5_500,
            snapshot_epoch=decision_epoch,
        ),
        _l2_row(
            date,
            ticker,
            sport,
            16_000_000 if stale_5s_outcome else 15_000_000,
            5,
            5_600,
            5_700,
            snapshot_epoch=decision_epoch,
        ),
        ]
    )
    return rows


def _build_namespace(
    root: Path,
    *,
    entry_depth: bool = True,
    entry_depth_null: bool = False,
    outcome_depth_null: bool = False,
    outcome_topology_null: bool = False,
    validation_flip: bool = False,
    future_train_trade: bool = False,
    negative_train_trade: bool = False,
    wrong_validate_trade_date: bool = False,
    validation_extra_sport: bool = False,
    train_extra_unadmitted_sport: bool = False,
    reconnect: bool = False,
    stale_5s_outcome: bool = False,
    null_bucket_attack: bool = False,
    trade_misbucket_attack: bool = False,
    real_w09_bucket_layout: bool = True,
) -> Path:
    namespace = root / f"source-{SOURCE_BINDING}"
    con = duckdb.connect()
    manifests: Dict[str, List[Dict[str, Any]]] = {
        "l2_replay": [],
        "trades_market": [],
    }
    for date in ALL_DATES:
        rows = _market_rows(
            date,
            entry_depth=entry_depth,
            entry_depth_null=entry_depth_null,
            outcome_depth_null=outcome_depth_null,
            outcome_topology_null=outcome_topology_null,
            validation_flip=validation_flip and date == "2026-07-17",
            reconnect=reconnect,
            stale_5s_outcome=stale_5s_outcome,
        )
        if validation_extra_sport and date == "2026-07-17":
            rows.extend(
                _market_rows(
                    date,
                    ticker="KXTEST-BASKETBALL",
                    sport="Basketball",
                )
            )
        if train_extra_unadmitted_sport and date in (
            "2026-07-12",
            "2026-07-15",
        ):
            rows.extend(
                _market_rows(
                    date,
                    ticker="KXTEST-SOCCER",
                    sport="Soccer",
                )
            )
        l2_bucket_count = 16 if real_w09_bucket_layout else 1
        l2_rows_by_bucket: Dict[int, List[List[Any]]] = {
            bucket: [] for bucket in range(l2_bucket_count)
        }
        for row in rows:
            bucket = int(
                con.execute(
                    f"SELECT mod(hash(?),{l2_bucket_count})", [row[4]]
                ).fetchone()[0]
            )
            l2_rows_by_bucket[bucket].append(row)
        if null_bucket_attack:
            l2_rows_by_bucket[l2_bucket_count] = [list(rows[0])]
        for bucket in range(l2_bucket_count + 1):
            # The final contiguous L2 bucket is reserved for NULL market_ticker.
            manifests["l2_replay"].append(
                _write_partition(
                    con,
                    namespace,
                    "l2_replay",
                    f"date={date}_bucket={bucket:03d}",
                    L2_SCHEMA,
                    (
                        l2_rows_by_bucket[l2_bucket_count]
                        if bucket == l2_bucket_count and null_bucket_attack
                        else l2_rows_by_bucket.get(bucket, [])
                    ),
                )
            )
        trade_date = (
            "2026-07-16"
            if wrong_validate_trade_date and date == "2026-07-17"
            else date
        )
        side = "no" if validation_flip and date == "2026-07-17" else "yes"
        trade_rows: List[List[Any]] = [
            [trade_date, 9_500_000, "KXTEST-TENNIS", 20_000, side]
        ]
        if future_train_trade and date in ("2026-07-12", "2026-07-15"):
            trade_rows.append(
                [trade_date, 10_000_001, "KXTEST-TENNIS", 9_000_000, "no"]
            )
        if negative_train_trade and date in ("2026-07-12", "2026-07-15"):
            trade_rows.append(
                [trade_date, 9_600_000, "KXTEST-TENNIS", -9_000_000, "no"]
            )
        if validation_extra_sport and date == "2026-07-17":
            trade_rows.append(
                [trade_date, 9_500_000, "KXTEST-BASKETBALL", 20_000, "yes"]
            )
        if train_extra_unadmitted_sport and date in (
            "2026-07-12",
            "2026-07-15",
        ):
            trade_rows.append(
                [trade_date, 9_500_000, "KXTEST-SOCCER", 20_000, "yes"]
            )
        trade_bucket_count = 32 if real_w09_bucket_layout else 1
        trade_rows_by_bucket: Dict[int, List[List[Any]]] = {
            bucket: [] for bucket in range(trade_bucket_count)
        }
        for row in trade_rows:
            bucket = int(
                con.execute(
                    f"SELECT mod(hash(?),{trade_bucket_count})", [row[2]]
                ).fetchone()[0]
            )
            if trade_misbucket_attack and row is trade_rows[0]:
                bucket = (bucket + 1) % trade_bucket_count
            trade_rows_by_bucket[bucket].append(row)
        for bucket in range(trade_bucket_count):
            manifests["trades_market"].append(
                _write_partition(
                    con,
                    namespace,
                    "trades_market",
                    f"date={date}_bucket={bucket:02d}",
                    TRADE_SCHEMA,
                    trade_rows_by_bucket[bucket],
                )
            )
    con.close()
    for stage, partitions in manifests.items():
        manifest = {
            "schema_version": "deep03-bounded-stage-manifest-v1",
            "state": "COMPLETE",
            "stage": stage,
            "stage_version": f"{stage}-synthetic-v1",
            "source_binding": SOURCE_BINDING,
            "partition_count": len(partitions),
            "row_count": sum(row["row_count"] for row in partitions),
            "partitions": partitions,
        }
        manifest_path = namespace / stage / "MANIFEST.json"
        manifest_path.write_bytes(_canonical_bytes(manifest))
    return namespace


def _run(
    namespace: Path, output: Path, *, keep_scratch: bool = False
) -> Dict[str, Any]:
    return run_event_study(
        namespace,
        output,
        keep_scratch=keep_scratch,
        signal_quantile=0.90,
        min_train_candidates_per_sport=1,
    )


def _result(
    receipt: Dict[str, Any],
    *,
    date: str,
    control: str,
    horizon_us: int,
    sport: str = "_ALL_LOCKED_SPORTS",
) -> Dict[str, Any]:
    return next(
        row
        for row in receipt["results"]
        if row["date"] == date
        and row["control"] == control
        and row["horizon_us"] == horizon_us
        and row["sport"] == sport
    )


OUTCOME_CLASSIFICATION_FIELDS = (
    "outside_observation_count",
    "stale_outcome_count",
    "invalid_outcome_book_count",
    "insufficient_exit_depth_count",
    "evaluable_count",
)


def _assert_single_classified_trigger(
    result: Dict[str, Any], expected_field: str
) -> None:
    assert result["trigger_count"] == 1
    assert result[expected_field] == 1
    assert sum(result[field] for field in OUTCOME_CLASSIFICATION_FIELDS) == 1
    assert result["trigger_outcome_classification_conserved"] is True


def test_end_to_end_outputs_executable_gross_controls_and_unknown_phase(tmp_path):
    namespace = _build_namespace(tmp_path / "checkpoint")
    output = tmp_path / "run"
    receipt = _run(namespace, output)

    assert (output / RECEIPT_NAME).is_file()
    assert (output / REPORT_NAME).is_file()
    assert (output / MODEL_SEAL_NAME).is_file()
    assert not (output / ".scratch").exists()
    assert receipt["model"]["train_dates"] == ["2026-07-12", "2026-07-15"]
    assert receipt["model"]["validation_dates_read_during_fit"] == []
    assert receipt["model"]["locked_sports"] == ["Tennis"]
    order = receipt["split_contract"]["execution_order"]
    assert order.index("SEAL_TRAIN_MODEL_COMPLETE") < order.index(
        "MATERIALIZE_VALIDATE_PAYLOAD_START"
    )
    assert (
        receipt["artifacts"]["train_model_seal"]["sha256"]
        == hashlib.sha256((output / MODEL_SEAL_NAME).read_bytes()).hexdigest()
    )
    assert receipt["claims"]["cash_pnl"] is False
    assert receipt["claims"]["phase"] == "UNKNOWN_PHASE_ONLY"

    baseline = _result(
        receipt,
        date="2026-07-17",
        control="BASELINE",
        horizon_us=100_000,
    )
    reverse = _result(
        receipt,
        date="2026-07-17",
        control="REVERSE",
        horizon_us=100_000,
    )
    placebo = _result(
        receipt,
        date="2026-07-17",
        control="PLACEBO_HASH_DIRECTION",
        horizon_us=100_000,
    )
    assert baseline["trigger_count"] == 1
    assert baseline["evaluable_count"] == 1
    assert baseline["mean_executable_gross_return_e4_per_contract"] == 100.0
    assert reverse["trigger_count"] == baseline["trigger_count"]
    assert reverse["mean_executable_gross_return_e4_per_contract"] == -400.0
    assert placebo["trigger_count"] == baseline["trigger_count"]


def test_validation_and_future_trade_cannot_change_train_model_or_past_flow(tmp_path):
    base_namespace = _build_namespace(tmp_path / "base-checkpoint")
    changed_namespace = _build_namespace(
        tmp_path / "changed-checkpoint",
        validation_flip=True,
        future_train_trade=True,
        validation_extra_sport=True,
        train_extra_unadmitted_sport=True,
    )
    base = _run(base_namespace, tmp_path / "base-run", keep_scratch=True)
    changed = _run(
        changed_namespace, tmp_path / "changed-run", keep_scratch=True
    )

    # generated output lineage differs, but the TRAIN-only model is byte stable.
    assert base["model"] == changed["model"]
    assert changed["model"]["locked_sports"] == ["Tennis"]
    validate_diag = next(
        row
        for row in changed["diagnostics"]["by_date"]
        if row["date"] == "2026-07-17"
    )
    assert validate_diag["validation_rows_outside_locked_sport_universe"] == 1

    con = duckdb.connect()
    feature_path = None
    # Empty hash shards sort first; locate the actual decision row.
    for candidate in (
        tmp_path / "changed-run" / ".scratch" / "features"
    ).glob("date=2026-07-12_bucket=*.parquet"):
        if con.execute(
            "SELECT count(*) FROM read_parquet(?)", [str(candidate)]
        ).fetchone()[0]:
            feature_path = candidate
            break
    assert feature_path is not None
    signed_flow, total_flow = con.execute(
        "SELECT signed_flow_e4,total_flow_e4 FROM read_parquet(?)",
        [str(feature_path)],
    ).fetchone()
    con.close()
    # The enormous NO trade at decision+1us is future data and is excluded.
    assert signed_flow == 20_000
    assert total_flow == 20_000


def test_partition_date_escape_is_refused(tmp_path):
    namespace = _build_namespace(
        tmp_path / "checkpoint", wrong_validate_trade_date=True
    )
    with pytest.raises(H4StudyError, match="date escape"):
        _run(namespace, tmp_path / "run")
    # VALIDATE was not opened until the TRAIN model was physically sealed.
    assert (tmp_path / "run" / MODEL_SEAL_NAME).is_file()


def test_real_w09_two_digit_trade_and_three_digit_l2_layout(tmp_path):
    namespace = _build_namespace(
        tmp_path / "checkpoint", real_w09_bucket_layout=True
    )
    receipt = _run(namespace, tmp_path / "run")

    assert receipt["source"]["l2_replay"]["market_bucket_count"] == 16
    assert receipt["source"]["trades_market"]["market_bucket_count"] == 32
    assert (
        namespace
        / "l2_replay"
        / "data"
        / "date=2026-07-12_bucket=016.parquet"
    ).is_file()
    assert (
        namespace
        / "trades_market"
        / "data"
        / "date=2026-07-12_bucket=31.parquet"
    ).is_file()
    result = _result(
        receipt,
        date="2026-07-17",
        control="BASELINE",
        horizon_us=100_000,
    )
    assert result["trigger_count"] == 1


@pytest.mark.parametrize(
    "depth_kwargs",
    [
        pytest.param({"entry_depth": False}, id="zero-entry-depth"),
        pytest.param({"entry_depth_null": True}, id="null-entry-depth"),
    ],
)
def test_zero_or_null_depth_and_zero_trigger_denominators_are_explicit(
    tmp_path, depth_kwargs
):
    namespace = _build_namespace(
        tmp_path / "checkpoint", **depth_kwargs
    )
    receipt = _run(namespace, tmp_path / "run")

    assert receipt["model"]["enabled"] is False
    assert receipt["model"]["disabled_reason"] == "NO_TRAIN_LOCKED_SPORTS"
    by_date = {
        row["date"]: row for row in receipt["diagnostics"]["by_date"]
    }
    assert by_date["2026-07-12"]["raw_decisions"] == 1
    assert by_date["2026-07-12"]["entry_depth_ok"] == 0
    assert by_date["2026-07-12"]["insufficient_entry_depth"] == 1
    assert by_date["2026-07-12"]["feature_eligible"] == 0
    assert by_date["2026-07-12"]["entry_depth_classification_conserved"] is True
    assert (
        by_date["2026-07-17"][
            "validation_rows_outside_locked_sport_universe"
        ]
        == 1
    )
    zero = _result(
        receipt,
        date="2026-07-17",
        control="BASELINE",
        horizon_us=100_000,
    )
    assert zero["raw_decision_count"] == 0
    assert zero["trigger_count"] == 0
    assert zero["zero_trigger"] is True
    assert zero["trigger_rate_per_feature_eligible"] is None


def test_reconnect_snapshot_is_not_a_signal_and_cannot_reuse_old_epoch_quote(
    tmp_path,
):
    namespace = _build_namespace(
        tmp_path / "checkpoint",
        reconnect=True,
    )
    receipt = _run(namespace, tmp_path / "run")
    by_date = {
        row["date"]: row for row in receipt["diagnostics"]["by_date"]
    }

    for date in ALL_DATES:
        # Only the post-reconnect DELTA is a decision; the top-changing
        # SNAPSHOT_APPLIED row remains an anchor.
        assert by_date[date]["raw_decisions"] == 1
        # The quote exactly one second old belongs to epoch 1 and cannot be
        # joined to the epoch-2 decision.
        assert by_date[date]["no_past_quote"] == 1
        assert by_date[date]["feature_eligible"] == 0
    assert receipt["model"]["locked_sports"] == []
    assert receipt["model"]["disabled_reason"] == "NO_TRAIN_LOCKED_SPORTS"


@pytest.mark.parametrize(
    ("build_kwargs", "expected_field"),
    [
        pytest.param(
            {"outcome_depth_null": True},
            "insufficient_exit_depth_count",
            id="null-exit-depth",
        ),
        pytest.param(
            {"outcome_topology_null": True},
            "invalid_outcome_book_count",
            id="null-exit-topology",
        ),
    ],
)
def test_null_outcomes_are_fully_classified_and_conserved(
    tmp_path, build_kwargs, expected_field
):
    namespace = _build_namespace(
        tmp_path / "checkpoint",
        **build_kwargs,
    )
    receipt = _run(namespace, tmp_path / "run")
    result = _result(
        receipt,
        date="2026-07-17",
        control="BASELINE",
        horizon_us=100_000,
    )

    _assert_single_classified_trigger(result, expected_field)
    assert result["evaluable_count"] == 0
    assert result["evaluable_rate_per_trigger"] == 0.0
    assert result["mean_executable_gross_return_e4_per_contract"] is None


def test_stale_five_second_outcome_is_not_evaluable_and_remains_sensitivity(
    tmp_path,
):
    namespace = _build_namespace(
        tmp_path / "checkpoint",
        stale_5s_outcome=True,
    )
    receipt = _run(namespace, tmp_path / "run")
    result = _result(
        receipt,
        date="2026-07-17",
        control="BASELINE",
        horizon_us=5_000_000,
    )

    _assert_single_classified_trigger(result, "stale_outcome_count")
    assert result["evaluable_count"] == 0
    assert result["evaluable_rate_per_trigger"] == 0.0
    assert receipt["claims"]["primary_horizons_us"] == [100_000, 1_000_000]
    assert receipt["claims"][
        "sensitivity_displayed_state_proxy_horizons_us"
    ] == [5_000_000]
    five_second = next(
        row for row in receipt["horizons"] if row["horizon_us"] == 5_000_000
    )
    assert five_second["role"] == "SENSITIVITY_DISPLAYED_STATE_PROXY"


@pytest.mark.parametrize(
    ("build_kwargs", "error_match"),
    [
        pytest.param(
            {"negative_train_trade": True},
            "invalid non-positive/unsigned trade rows",
            id="negative-train-trade",
        ),
        pytest.param(
            {"null_bucket_attack": True},
            "reserved L2 NULL bucket contains a market",
            id="non-null-market-in-reserved-bucket",
        ),
        pytest.param(
            {"trade_misbucket_attack": True},
            "checkpoint hash partition mismatch",
            id="trade-hash-misbucket",
        ),
    ],
)
def test_adversarial_checkpoint_rows_fail_closed(
    tmp_path, build_kwargs, error_match
):
    namespace = _build_namespace(
        tmp_path / "checkpoint",
        **build_kwargs,
    )
    output = tmp_path / "run"

    with pytest.raises(H4StudyError, match=error_match):
        _run(namespace, output)
    assert not (output / RECEIPT_NAME).exists()
    assert not (output / MODEL_SEAL_NAME).exists()
