from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from tools.research.c1_real_fill_runner import (
    CAMPAIGN_COLUMNS,
    FILL_COLUMNS,
    MARKOUT_COLUMNS,
    C1RunnerError,
    PartitionBundle,
    PartitionInput,
    _load_config,
    _copy_parquet,
    _register_dicts,
    _trade_rows_with_state,
    aggregate_outputs,
    activation_states,
    allocate_and_fill,
    expand_variants,
    process_partition,
    suppress_overlaps,
)
from tools.research import c1_real_fill_report


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "c1_real_fill_v1.json"


def _episode(
    episode_id: str,
    depletion_ns: int,
    *,
    market: str = "MKT",
    side: str = "yes",
    quote: int = 5_000,
    epoch: int = 1,
) -> dict[str, object]:
    return {
        "episode_id": episode_id,
        "date": "2026-07-12",
        "market_ticker": market,
        "event_proxy": "EVT",
        "sport": "Baseball",
        "family": "SERIES",
        "side": side,
        "depletion_ns": depletion_ns,
        "refill_ns": None,
        "refill_fraction": None,
        "original_touch_price_e4": quote,
        "pre_touch_qty_e4": 20_000,
        "removed_e4": 10_000,
        "depletion_fraction": 0.5,
        "snapshot_epoch": epoch,
    }


def _eligible_campaign(
    campaign_id: str,
    depletion_ns: int,
    activation_us: int,
    cancel_us: int,
) -> dict[str, object]:
    return {
        "campaign_id": campaign_id,
        "episode_id": campaign_id,
        "date": "2026-07-12",
        "market_ticker": "MKT",
        "event_proxy": "EVT",
        "sport": "Baseball",
        "family": "SERIES",
        "side": "yes",
        "quote_price_e4": 5_000,
        "depletion_ns": depletion_ns,
        "snapshot_epoch": 1,
        "state_epoch": 1,
        "latency_id": "PRIMARY",
        "place_us": 50_000,
        "cancel_us": 50_000,
        "cancel_timer_us": 200_000,
        "activation_ns": activation_us * 1_000,
        "cancel_intent_ns": (cancel_us - 50_000) * 1_000,
        "cancel_effective_ns": cancel_us * 1_000,
        "refill_group": "NON_REFILL_OR_CENSORED",
        "queue_status": "IMPROVED_NEW_TOUCH",
        "queue_ahead_e4": 0,
    }


def _trade(trade_id: str, t_us: int, *, price: int = 4_900) -> dict[str, object]:
    return {
        "date": "2026-07-12",
        "t_us": t_us,
        "market_ticker": "MKT",
        "event_proxy": "EVT",
        "sport": "Baseball",
        "trade_id": trade_id,
        "yes_price_e4": price,
        "count_e4": 10_000,
        "taker_side": "no",
        "state_ns": t_us * 1_000,
        "state_classification": "DELTA_APPLIED",
        "state_epoch": 1,
        "state_book_valid": True,
        "state_topology": "TWO_SIDED",
        "state_bid_e4": 4_900,
        "state_ask_e4": 5_100,
    }


def _write_parquet(con, path: Path, create_sql: str, rows: list[tuple]) -> None:
    con.execute("DROP TABLE IF EXISTS fixture")
    con.execute("CREATE TABLE fixture(" + create_sql + ")")
    if rows:
        con.executemany(
            "INSERT INTO fixture VALUES (" + ",".join("?" for _ in rows[0]) + ")",
            rows,
        )
    quoted = "'" + str(path).replace("'", "''") + "'"
    con.execute(f"COPY fixture TO {quoted} (FORMAT PARQUET)")


def _partition(stage: str, key: str, path: Path, rows: int) -> PartitionInput:
    return PartitionInput(
        stage=stage,
        partition_key=key,
        path=path,
        sha256="0" * 64,
        size_bytes=path.stat().st_size,
        row_count=rows,
        receipt_sha256="1" * 64,
    )


def test_greedy_overlap_is_closed_at_one_second_and_validates_trigger() -> None:
    rows = [
        _episode("e1", 1_000_000_000),
        _episode("e2", 2_000_000_000),  # exactly t0+1s: suppressed
        _episode("e3", 2_000_000_001),
    ]
    accepted, excluded = suppress_overlaps(rows, campaign_horizon_us=1_000_000)
    assert [row["episode_id"] for row in accepted] == ["e1", "e3"]
    assert [row["reason"] for row in excluded] == ["OVERLAP_SUPPRESSED"]

    invalid = _episode("bad", 3_000_000_000)
    invalid["removed_e4"] = 9_999
    with pytest.raises(C1RunnerError, match="quantities"):
        suppress_overlaps([invalid], campaign_horizon_us=1_000_000)


def test_variant_cancel_boundaries_are_frozen() -> None:
    config = _load_config(CONFIG_PATH)
    row = _episode("e1", 1_000_000_000)
    row["refill_ns"] = 1_200_000_000
    row["refill_fraction"] = 0.8
    variants = expand_variants([row], config)
    fast_200 = next(
        item
        for item in variants
        if item["latency_id"] == "FAST" and item["cancel_timer_us"] == 200_000
    )
    assert fast_200["activation_ns"] == 1_005_000_000
    assert fast_200["cancel_intent_ns"] == 2_000_000_000
    assert fast_200["cancel_effective_ns"] == 2_005_000_000


def test_full_order_releases_later_trades_for_next_campaign() -> None:
    config = _load_config(CONFIG_PATH)
    campaigns = [
        _eligible_campaign("c1", 0, 100, 2_000),
        _eligible_campaign("c2", 1_000_000, 1_000, 3_000),
    ]
    fills, qc = allocate_and_fill(
        campaigns,
        [_trade("t1", 500), _trade("t2", 1_500)],
        config,
    )
    strict = [row for row in fills if row["track"] == "STRICT_THROUGH"]
    assert [(row["campaign_id"], row["trade_id"]) for row in strict] == [
        ("c1", "t1"),
        ("c2", "t2"),
    ]
    assert qc["duplicate_allocation_count"] == 0


def test_activation_post_only_and_epoch_fail_closed(tmp_path: Path) -> None:
    con = duckdb.connect()
    replay = tmp_path / "replay.parquet"
    _write_parquet(
        con,
        replay,
        "market_ticker VARCHAR,recv_wall_ns BIGINT,recv_mono_ns BIGINT,"
        "ws_sid BIGINT,ws_seq BIGINT,classification VARCHAR,snapshot_epoch BIGINT,"
        "book_valid BOOLEAN,topology VARCHAR,bid_e4 BIGINT,bid_qty_e4 BIGINT,"
        "ask_e4 BIGINT,ask_qty_e4 BIGINT",
        [
            (
                "MKT",
                1_000_000_000,
                1_000_000_000,
                1,
                1,
                "DELTA_APPLIED",
                2,
                True,
                "TWO_SIDED",
                4_900,
                10_000,
                5_000,
                10_000,
            )
        ],
    )
    config = _load_config(CONFIG_PATH)
    variant = expand_variants([_episode("e", 1_000_000_000)], config)[0]
    eligible, excluded = activation_states(con, [variant], replay, ttl_us=250_000)
    assert eligible == []
    assert excluded[0]["reason"] == "ACTIVATION_EPOCH_MISMATCH"

    variant["snapshot_epoch"] = 2
    eligible, excluded = activation_states(con, [variant], replay, ttl_us=250_000)
    assert eligible == []
    assert excluded[0]["reason"] == "POST_ONLY_REJECTED"


def test_trade_partition_wrong_date_fails_closed(tmp_path: Path) -> None:
    con = duckdb.connect()
    schema = (
        "date DATE,t_us BIGINT,market_ticker VARCHAR,event_proxy VARCHAR,"
        "sport VARCHAR,trade_id VARCHAR,yes_price_e4 BIGINT,count_e4 BIGINT,"
        "taker_side VARCHAR"
    )
    wrong = tmp_path / "wrong.parquet"
    empty = tmp_path / "empty.parquet"
    _write_parquet(
        con,
        wrong,
        schema,
        [("2026-07-15", 500, "MKT", "EVT", "Baseball", "bad", 4_900, 10_000, "no")],
    )
    _write_parquet(con, empty, schema, [])
    campaign = _eligible_campaign("c1", 0, 100, 2_000)
    with pytest.raises(C1RunnerError, match="outside 2026-07-12"):
        _trade_rows_with_state(con, [wrong, empty], empty, [campaign])


def test_small_partition_runs_end_to_end(tmp_path: Path) -> None:
    con = duckdb.connect()
    config = _load_config(CONFIG_PATH)
    t0_ns = 1_000_000_000_000
    episode_path = tmp_path / "episodes.parquet"
    replay_path = tmp_path / "replay.parquet"
    trade_a = tmp_path / "trade_a.parquet"
    trade_b = tmp_path / "trade_b.parquet"
    episode = _episode("e1", t0_ns)
    _write_parquet(
        con,
        episode_path,
        "episode_id VARCHAR,date DATE,market_ticker VARCHAR,event_proxy VARCHAR,"
        "sport VARCHAR,family VARCHAR,side VARCHAR,depletion_ns BIGINT,"
        "refill_ns BIGINT,refill_fraction DOUBLE,original_touch_price_e4 BIGINT,"
        "pre_touch_qty_e4 BIGINT,removed_e4 BIGINT,depletion_fraction DOUBLE,"
        "snapshot_epoch BIGINT",
        [
            (
                episode["episode_id"],
                episode["date"],
                episode["market_ticker"],
                episode["event_proxy"],
                episode["sport"],
                episode["family"],
                episode["side"],
                episode["depletion_ns"],
                None,
                None,
                episode["original_touch_price_e4"],
                episode["pre_touch_qty_e4"],
                episode["removed_e4"],
                episode["depletion_fraction"],
                episode["snapshot_epoch"],
            )
        ],
    )
    replay_rows = []
    for index, offset_us in enumerate(range(0, 1_700_000, 50_000), start=1):
        replay_rows.append(
            (
                "2026-07-12",
                (t0_ns // 1_000) + offset_us,
                t0_ns + offset_us * 1_000,
                t0_ns + offset_us * 1_000,
                "MKT",
                "EVT",
                "Baseball",
                "SERIES",
                1,
                index,
                "delta",
                "yes",
                4_900,
                1,
                "DELTA_APPLIED",
                1,
                True,
                "TWO_SIDED",
                4_900,
                10_000,
                5_100,
                10_000,
            )
        )
    _write_parquet(
        con,
        replay_path,
        "date DATE,t_us BIGINT,recv_wall_ns BIGINT,recv_mono_ns BIGINT,"
        "market_ticker VARCHAR,event_proxy VARCHAR,sport VARCHAR,family VARCHAR,"
        "ws_sid BIGINT,ws_seq BIGINT,msg_type VARCHAR,side VARCHAR,price_e4 BIGINT,"
        "delta_e4 BIGINT,classification VARCHAR,snapshot_epoch BIGINT,"
        "book_valid BOOLEAN,topology VARCHAR,bid_e4 BIGINT,bid_qty_e4 BIGINT,"
        "ask_e4 BIGINT,ask_qty_e4 BIGINT",
        replay_rows,
    )
    trade_schema = (
        "date DATE,t_us BIGINT,market_ticker VARCHAR,event_proxy VARCHAR,"
        "sport VARCHAR,trade_id VARCHAR,yes_price_e4 BIGINT,count_e4 BIGINT,"
        "taker_side VARCHAR"
    )
    _write_parquet(
        con,
        trade_a,
        trade_schema,
        [
            (
                "2026-07-12",
                (t0_ns // 1_000) + 60_000,
                "MKT",
                "EVT",
                "Baseball",
                "t1",
                4_900,
                10_000,
                "no",
            ),
            (
                "2026-07-12",
                (t0_ns // 1_000) + 600_000,
                "MKT",
                "EVT",
                "Baseball",
                "t2",
                4_900,
                10_000,
                "no",
            ),
        ],
    )
    _write_parquet(con, trade_b, trade_schema, [])
    bundle = PartitionBundle(
        date="2026-07-12",
        bucket=0,
        episodes=_partition("l2_episodes", "date=2026-07-12_bucket=000", episode_path, 1),
        replay=_partition("l2_replay", "date=2026-07-12_bucket=000", replay_path, len(replay_rows)),
        trades=(
            _partition("trades_market", "date=2026-07-12_bucket=00", trade_a, 2),
            _partition("trades_market", "date=2026-07-12_bucket=16", trade_b, 0),
        ),
    )
    output = tmp_path / "out"
    output.mkdir()
    receipt = process_partition(con, bundle, config, output)
    assert receipt["state"] == "COMPLETE"
    assert receipt["counts"]["episodes"] == 1
    assert receipt["counts"]["fill_slices"] > 0
    assert receipt["queue_invariants"]["duplicate_allocation_count"] == 0
    assert (output / "PARTITIONS/date=2026-07-12_bucket=00/FILL_SLICES.parquet").is_file()
    stored = json.loads(
        (output / "PARTITIONS/date=2026-07-12_bucket=00/PARTITION_RECEIPT.json").read_text()
    )
    assert stored["live_order_writes"] == 0
    tracks = {
        row[0]
        for row in con.execute(
            "SELECT DISTINCT track FROM read_parquet(?)",
            [str(output / "PARTITIONS/date=2026-07-12_bucket=00/FILL_SLICES.parquet")],
        ).fetchall()
    }
    assert "STRICT_THROUGH" in tracks


def test_aggregation_is_publisher_compatible_and_complete(tmp_path: Path) -> None:
    con = duckdb.connect()
    config = _load_config(CONFIG_PATH)
    output = tmp_path / "analysis"
    output.mkdir()
    receipts = []
    dates = config["eligible_dates"]
    for day in dates:
        for bucket in range(16):
            key = f"date={day}_bucket={bucket:02d}"
            directory = output / "PARTITIONS" / key
            directory.mkdir(parents=True)
            campaigns = []
            fills = []
            markouts = []
            if bucket == 0:
                for timer in (200_000, 300_000):
                    campaign_id = f"{day}|{timer}"
                    campaign = {
                        "campaign_id": campaign_id,
                        "episode_id": f"ep-{day}",
                        "date": day,
                        "market_ticker": f"MKT-{day}",
                        "event_proxy": f"EVT-{day}",
                        "sport": "Baseball",
                        "family": "SERIES",
                        "side": "yes",
                        "quote_price_e4": 5_000,
                        "depletion_ns": 1_000_000_000,
                        "snapshot_epoch": 1,
                        "latency_id": "PRIMARY",
                        "place_us": 50_000,
                        "cancel_us": 50_000,
                        "cancel_timer_us": timer,
                        "activation_ns": 1_050_000_000,
                        "cancel_intent_ns": 1_200_000_000,
                        "cancel_effective_ns": 1_250_000_000,
                        "refill_group": "NON_REFILL_OR_CENSORED",
                        "state_ns": 1_049_000_000,
                        "state_epoch": 1,
                        "queue_status": "IMPROVED_NEW_TOUCH",
                        "queue_ahead_e4": 0,
                    }
                    campaigns.append(campaign)
                    fill = {
                        "fill_slice_id": f"fill-{day}-{timer}",
                        "campaign_id": campaign_id,
                        "episode_id": campaign["episode_id"],
                        "date": day,
                        "market_ticker": campaign["market_ticker"],
                        "event_proxy": campaign["event_proxy"],
                        "sport": "Baseball",
                        "family": "SERIES",
                        "side": "yes",
                        "quote_price_e4": 5_000,
                        "snapshot_epoch": 1,
                        "latency_id": "PRIMARY",
                        "cancel_timer_us": timer,
                        "track": "STRICT_THROUGH",
                        "refill_group": "NON_REFILL_OR_CENSORED",
                        "trade_id": f"trade-{day}-{timer}",
                        "trade_us": 1_100_000,
                        "public_count_e4": 10_000,
                        "fill_count_e4": 10_000,
                        "fill_reason": "STRICT_THROUGH",
                        "queue_before_e4": None,
                        "queue_after_e4": None,
                    }
                    fills.append(fill)
                    for horizon in (100_000, 200_000, 500_000, 1_000_000):
                        markout = dict(fill)
                        markout.update(
                            {
                                "horizon_us": horizon,
                                "target_ns": 1_100_000_000 + horizon * 1_000,
                                "state_ns": 1_100_000_000 + horizon * 1_000,
                                "markout_status": "OBSERVED",
                                "gross_e4": 10,
                                "mid_twice_gross_e4": 20,
                            }
                        )
                        markouts.append(markout)
            _register_dicts(con, "agg_campaigns", CAMPAIGN_COLUMNS, campaigns)
            _register_dicts(con, "agg_fills", FILL_COLUMNS, fills)
            _register_dicts(con, "agg_markouts", MARKOUT_COLUMNS, markouts)
            campaign_path = directory / "CAMPAIGNS.parquet"
            fill_path = directory / "FILL_SLICES.parquet"
            markout_path = directory / "MARKOUTS.parquet"
            _copy_parquet(con, "agg_campaigns", campaign_path, "campaign_id")
            _copy_parquet(con, "agg_fills", fill_path, "fill_slice_id")
            _copy_parquet(con, "agg_markouts", markout_path, "fill_slice_id,horizon_us")
            receipts.append(
                {
                    "partition_key": key,
                    "date": day,
                    "counts": {"campaigns_accepted": 1 if bucket == 0 else 0},
                    "exclusion_reasons": {},
                    "queue_invariants": {
                        "duplicate_allocation_count": 0,
                        "public_volume_exceeded_count": 0,
                    },
                    "artifacts": {
                        "campaigns": {"path": campaign_path.relative_to(output).as_posix()},
                        "fill_slices": {"path": fill_path.relative_to(output).as_posix()},
                        "markouts": {"path": markout_path.relative_to(output).as_posix()},
                    },
                }
            )
    input_receipt = {
        "source_binding": config["source_binding"],
        "l2_availability": [
            {
                "date": day,
                "state": (
                    "CAPTURED_CLEAN_INCLUDED" if day in dates else "EXCLUDED_DATA_QUALITY"
                ),
                "source_rows": 0 if day in {"2026-07-10", "2026-07-11"} else 1,
                "eligible_for_estimands": day in dates,
            }
            for day in [
                "2026-07-10",
                "2026-07-11",
                "2026-07-12",
                "2026-07-13",
                "2026-07-14",
                "2026-07-15",
                "2026-07-16",
                "2026-07-17",
            ]
        ],
    }
    aggregates = aggregate_outputs(
        con,
        output_root=output,
        config=config,
        partition_receipts=receipts,
        input_receipt=input_receipt,
        config_path=CONFIG_PATH,
        run_started_monotonic=0.0,
    )
    c1_real_fill_report.validate_aggregates(aggregates)
    assert aggregates["verdict"]["code"] == "RETAIN_FOR_20_DAY_VALIDATION"
    assert aggregates["concentration"] == [
        {
            "date": day,
            "events": 1,
            "markets": 1,
            "max_market_share": 1.0,
            "hhi": 1.0,
        }
        for day in dates
    ]
    assert all(type(row["count"]) is int for row in aggregates["exclusions"])
    report_dir = tmp_path / "report"
    pdf_path = tmp_path / "output" / "pdf" / "C1_TEST.pdf"
    published = c1_real_fill_report.publish(output, report_dir, pdf_path)
    assert len(published["charts"]) == 5
    assert (report_dir / "index.html").is_file()
    assert pdf_path.stat().st_size > 10_000
    assert (output / "C1_RUN_COMPLETE.json").is_file()
    assert (output / "ANALYSIS_ARTIFACT_SHA256.json").is_file()
