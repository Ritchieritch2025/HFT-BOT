#!/usr/bin/env python3
"""Adversarial exactness tests for the Deep03 bounded reducers.

These tests deliberately put one logical stratum on both sides of a physical
market-hash boundary.  They protect the non-additive parts of the legacy
estimands: continuous quantiles and exact distinct sets must be reduced from
their narrow observations, never from per-shard summary rows.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import sys
from pathlib import Path
from typing import Any, Iterable

import duckdb
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "research"))

from deep03_v3_methods import (  # noqa: E402
    _reduce_b01_observations,
    _reduce_b02_intervals,
    _reduce_b04_observations,
    execute_all,
    execute_all_bounded,
)


L1_SCHEMA = """
  local_recv_ts_us BIGINT,recv_wall_ns BIGINT,recv_mono_ns BIGINT,
  ws_sid BIGINT,ws_seq BIGINT,market_ticker VARCHAR,event_ticker VARCHAR,
  series_ticker VARCHAR,subcategory VARCHAR,"group" VARCHAR,
  yes_bid_e4 BIGINT,yes_ask_e4 BIGINT,yes_bid_qty_e4 BIGINT,
  yes_ask_qty_e4 BIGINT
"""

TRADE_SCHEMA = """
  local_recv_ts_us BIGINT,recv_wall_ns BIGINT,recv_mono_ns BIGINT,
  market_ticker VARCHAR,event_ticker VARCHAR,series_ticker VARCHAR,
  subcategory VARCHAR,"group" VARCHAR,trade_id VARCHAR,yes_price_e4 BIGINT,
  no_price_e4 BIGINT,count_e4 BIGINT,taker_side VARCHAR
"""


def _write_rows(
    path: Path,
    *,
    schema: str,
    rows: Iterable[tuple[Any, ...]],
    table: str = "fixture",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(f"CREATE TABLE {table}({schema})")
        materialized = list(rows)
        if materialized:
            width = len(materialized[0])
            con.executemany(
                f"INSERT INTO {table} VALUES ({','.join('?' for _ in range(width))})",
                materialized,
            )
        quoted = str(path).replace("'", "''")
        con.execute(f"COPY {table} TO '{quoted}' (FORMAT PARQUET)")
    finally:
        con.close()


def _row(rows: list[dict[str, Any]], **keys: Any) -> dict[str, Any]:
    matches = [
        row
        for row in rows
        if all(row.get(key) == value for key, value in keys.items())
    ]
    assert len(matches) == 1, (keys, rows)
    return matches[0]


def test_b01_reducer_uses_global_observations_for_odd_even_quantiles_and_distincts(
    tmp_path: Path,
) -> None:
    schema = """
      date DATE,event_proxy VARCHAR,market_ticker VARCHAR,sport VARCHAR,
      phase VARCHAR,horizon_us BIGINT,signed_markout_logodds DOUBLE,
      pre_mid_e4 DOUBLE,spread_logodds DOUBLE
    """
    first = tmp_path / "b01-p00.parquet"
    second = tmp_path / "b01-p01.parquet"
    _write_rows(
        first,
        schema=schema,
        rows=[
            (
                "2026-07-16",
                "E1",
                "M1",
                "Tennis",
                "PRE",
                1_000_000,
                -2.0,
                100.0,
                0.1,
            ),
            (
                "2026-07-16",
                "E1",
                "M2",
                "Tennis",
                "PRE",
                1_000_000,
                -1.0,
                200.0,
                0.2,
            ),
            (
                "2026-07-16",
                "E1",
                "M1",
                "Tennis",
                "PRE",
                5_000_000,
                -1.0,
                100.0,
                0.1,
            ),
            (
                "2026-07-16",
                "E1",
                "M2",
                "Tennis",
                "PRE",
                5_000_000,
                1.0,
                200.0,
                0.2,
            ),
        ],
    )
    _write_rows(
        second,
        schema=schema,
        rows=[
            # M1/E1 recur in another day: neither markets nor events nor days
            # may be summed from per-file counts.
            ("2026-07-17", "E1", "M1", "Tennis", "PRE", 1_000_000, 0.0, 300.0, 0.3),
            ("2026-07-17", "E2", "M3", "Tennis", "PRE", 1_000_000, 3.0, 400.0, 0.4),
            ("2026-07-17", "E2", "M4", "Tennis", "PRE", 1_000_000, 10.0, 500.0, 0.5),
            ("2026-07-17", "E1", "M1", "Tennis", "PRE", 5_000_000, 3.0, 300.0, 0.3),
            ("2026-07-17", "E2", "M3", "Tennis", "PRE", 5_000_000, 9.0, 400.0, 0.4),
        ],
    )

    con = duckdb.connect()
    try:
        rows = _reduce_b01_observations(con, [first, second])
    finally:
        con.close()

    odd = _row(rows, sport="Tennis", phase="PRE", horizon_us=1_000_000)
    assert odd["n"] == 5
    assert odd["mean_signed_markout_logodds"] == pytest.approx(2.0)
    assert odd["p50_signed_markout_logodds"] == pytest.approx(0.0)
    assert odd["mean_pre_mid_e4"] == pytest.approx(300.0)
    assert odd["mean_spread_logodds"] == pytest.approx(0.3)
    assert (odd["root_event_proxies"], odd["markets"], odd["days"]) == (2, 4, 2)

    even = _row(rows, sport="Tennis", phase="PRE", horizon_us=5_000_000)
    assert even["n"] == 4
    assert even["mean_signed_markout_logodds"] == pytest.approx(3.0)
    assert even["p50_signed_markout_logodds"] == pytest.approx(2.0)
    assert (even["root_event_proxies"], even["markets"], even["days"]) == (2, 3, 2)


def test_b02_reducer_computes_exact_interpolated_p95_and_set_unions(
    tmp_path: Path,
) -> None:
    schema = """
      date DATE,t_us BIGINT,market_ticker VARCHAR,event_proxy VARCHAR,
      sport VARCHAR,book_state VARCHAR,right_censored BOOLEAN,
      starts_in_gap BOOLEAN,interval_end_us BIGINT,duration_us BIGINT,
      phase VARCHAR
    """
    first = tmp_path / "b02-p00.parquet"
    second = tmp_path / "b02-p01.parquet"
    _write_rows(
        first,
        schema=schema,
        rows=[
            ("2026-07-16", 0, "M1", "E1", "Tennis", "ONE_SIDED", False, False, 1, 1_000_000, "PRE"),
            ("2026-07-16", 0, "M2", "E1", "Tennis", "ONE_SIDED", False, False, 1, 2_000_000, "PRE"),
            ("2026-07-16", 0, "M1", "E1", "Tennis", "TWO_SIDED", False, False, 1, 1_000_000, "PRE"),
            # Both rows are outside the legacy summary predicate.
            ("2026-07-16", 0, "MX", "EX", "Tennis", "INVALID_BOOK", False, False, 1, 99_000_000, "PRE"),
            ("2026-07-16", 0, "MX", "EX", "Tennis", "ONE_SIDED", True, False, 0, 0, "PRE"),
        ],
    )
    _write_rows(
        second,
        schema=schema,
        rows=[
            ("2026-07-17", 0, "M1", "E1", "Tennis", "ONE_SIDED", False, False, 1, 3_000_000, "PRE"),
            ("2026-07-17", 0, "M3", "E2", "Tennis", "ONE_SIDED", False, False, 1, 4_000_000, "PRE"),
            ("2026-07-17", 0, "M2", "E1", "Tennis", "TWO_SIDED", False, False, 1, 2_000_000, "PRE"),
            ("2026-07-17", 0, "M3", "E2", "Tennis", "TWO_SIDED", False, False, 1, 3_000_000, "PRE"),
        ],
    )

    con = duckdb.connect()
    try:
        rows = _reduce_b02_intervals(con, [first, second])
    finally:
        con.close()

    one = _row(rows, sport="Tennis", phase="PRE", book_state="ONE_SIDED")
    assert one["intervals"] == 4
    assert one["exposure_seconds"] == pytest.approx(10.0)
    assert one["p50_duration_seconds"] == pytest.approx(2.5)
    # quantile_cont uses (n-1)*p interpolation: 3 + 0.85*(4-3) = 3.85.
    assert one["p95_duration_seconds"] == pytest.approx(3.85)
    assert (one["markets"], one["root_event_proxies"], one["days"]) == (3, 2, 2)

    two = _row(rows, sport="Tennis", phase="PRE", book_state="TWO_SIDED")
    assert two["intervals"] == 3
    assert two["exposure_seconds"] == pytest.approx(6.0)
    assert two["p50_duration_seconds"] == pytest.approx(2.0)
    assert two["p95_duration_seconds"] == pytest.approx(2.9)
    assert (two["markets"], two["root_event_proxies"], two["days"]) == (3, 2, 2)


def test_b04_reducer_unions_same_event_minute_across_market_buckets(
    tmp_path: Path,
) -> None:
    schema = """
      date DATE,event_proxy VARCHAR,market_ticker VARCHAR,sport VARCHAR,
      phase VARCHAR,minute_id BIGINT,trades BIGINT,contracts_e4 BIGINT
    """
    first = tmp_path / "b04-p00.parquet"
    second = tmp_path / "b04-p01.parquet"
    _write_rows(
        first,
        schema=schema,
        rows=[
            ("2026-07-16", "E1", "M1", "Tennis", "PRE", 60, 1, 100),
            ("2026-07-16", "E1", "M2", "Tennis", "PRE", 60, 2, 200),
        ],
    )
    _write_rows(
        second,
        schema=schema,
        rows=[
            # E1:60 already exists in the other bucket and counts once.
            ("2026-07-16", "E1", "M3", "Tennis", "PRE", 60, 0, 0),
            ("2026-07-16", "E2", "M4", "Tennis", "PRE", 60, 3, 300),
        ],
    )

    con = duckdb.connect()
    try:
        rows = _reduce_b04_observations(con, [first, second])
    finally:
        con.close()

    row = _row(
        rows,
        date="2026-07-16",
        sport="Tennis",
        phase="PRE",
        utc_hour=1,
    )
    assert row["active_market_minutes"] == 4
    assert row["active_event_minutes"] == 2
    assert (row["markets"], row["root_event_proxies"]) == (4, 2)
    assert (row["trades"], row["contracts_e4"]) == (6, 600)
    assert row["trades_per_active_market_minute"] == pytest.approx(1.5)


def _epoch_us(day: str, hour: int = 1) -> int:
    instant = dt.datetime.fromisoformat(day).replace(
        hour=hour, tzinfo=dt.timezone.utc
    )
    return int(instant.timestamp() * 1_000_000)


def _state_stream(
    market: str,
    event: str,
    start_us: int,
    one_sided_seconds: int | None,
    two_sided_seconds: int,
) -> list[tuple[Any, ...]]:
    common = (market, event, "SER", "Tennis", "ATP")
    rows: list[tuple[Any, ...]] = []
    sequence = 1
    current = start_us
    if one_sided_seconds is not None:
        rows.append(
            (
                current,
                sequence,
                sequence,
                1,
                sequence,
                *common,
                4000,
                None,
                10_000,
                None,
            )
        )
        current += one_sided_seconds * 1_000_000
        sequence += 1
    rows.append(
        (
            current,
            sequence,
            sequence,
            1,
            sequence,
            *common,
            4000,
            4200,
            10_000,
            10_000,
        )
    )
    current += two_sided_seconds * 1_000_000
    sequence += 1
    rows.append(
        (
            current,
            sequence,
            sequence,
            1,
            sequence,
            *common,
            None,
            None,
            None,
            None,
        )
    )
    return rows


def _markout_stream(
    market: str,
    event: str,
    start_us: int,
    quotes: tuple[tuple[int, int], tuple[int, int], tuple[int, int]],
) -> list[tuple[Any, ...]]:
    offsets = (0, 2_000_000, 6_000_000)
    rows = []
    for sequence, (offset, (bid, ask)) in enumerate(zip(offsets, quotes), start=1):
        rows.append(
            (
                start_us + offset,
                sequence,
                sequence,
                2,
                sequence,
                market,
                event,
                "SER-B01",
                "Soccer",
                "MLS",
                bid,
                ask,
                10_000,
                10_000,
            )
        )
    # A final invalid update gives the +6s book a finite B02/B04 interval.
    rows.append(
        (
            start_us + 35_000_000,
            4,
            4,
            2,
            4,
            market,
            event,
            "SER-B01",
            "Soccer",
            "MLS",
            None,
            None,
            None,
            None,
        )
    )
    return rows


def _trade(
    at_us: int,
    market: str,
    event: str,
    trade_id: str,
    *,
    sport: str,
    price: int = 4200,
    contracts: int = 100,
) -> tuple[Any, ...]:
    return (
        at_us,
        at_us * 1000,
        at_us,
        market,
        event,
        "SER",
        sport,
        "LEAGUE",
        trade_id,
        price,
        10_000 - price,
        contracts,
        "yes",
    )


def _end_to_end_manifest(tmp_path: Path) -> dict[str, Any]:
    days = ("2026-07-16", "2026-07-17")
    l1_by_day: dict[str, list[tuple[Any, ...]]] = {day: [] for day in days}
    trades_by_day: dict[str, list[tuple[Any, ...]]] = {day: [] for day in days}

    d16 = _epoch_us(days[0])
    d17 = _epoch_us(days[1])
    # The shared event is intentionally spread over both market hash buckets.
    # DuckDB's fixed hash ABI maps T-0/T-1/T-2 to bucket 1 and T-5/T-6 to 0.
    l1_by_day[days[0]] += _state_stream("T-0", "E-SHARED", d16, 1, 1)
    l1_by_day[days[0]] += _state_stream("T-5", "E-SHARED", d16, 2, 2)
    l1_by_day[days[0]] += _state_stream("T-1", "E-SHARED", d16, 3, 3)
    l1_by_day[days[1]] += _state_stream("T-6", "E-SHARED", d17, 4, 4)
    l1_by_day[days[1]] += _state_stream("T-2", "E-SHARED", d17, None, 5)

    # B01's stratum repeats one event across two markets and two dates, so all
    # three distinct counts require a global set union.
    l1_by_day[days[0]] += _markout_stream(
        "B01-A", "E-B01", d16, ((4000, 4200), (4100, 4300), (4200, 4400))
    )
    l1_by_day[days[0]] += _markout_stream(
        "B01-B", "E-B01", d16, ((5000, 5200), (4900, 5100), (4800, 5000))
    )
    l1_by_day[days[1]] += _markout_stream(
        "B01-A", "E-B01", d17, ((6000, 6200), (6000, 6200), (6000, 6200))
    )

    trades_by_day[days[0]] += [
        _trade(d16 + 500_000, "T-0", "E-SHARED", "unique-d16-a", sport="Tennis"),
        _trade(d16 + 500_000, "T-5", "E-SHARED", "unique-d16-b", sport="Tennis"),
        _trade(d16 + 500_000, "T-0", "E-SHARED", "cross-date-id", sport="Tennis"),
        _trade(d16 + 1_000_000, "B01-A", "E-B01", "markout-d16-a", sport="Soccer"),
        _trade(d16 + 1_000_000, "B01-B", "E-B01", "markout-d16-b", sport="Soccer"),
    ]
    trades_by_day[days[1]] += [
        _trade(d17 + 500_000, "T-6", "E-SHARED", "unique-d17", sport="Tennis"),
        # The same ID on another date is a global economic conflict.  A
        # date-local implementation would wrongly admit both rows.
        _trade(d17 + 500_000, "T-6", "E-SHARED", "cross-date-id", sport="Tennis"),
        _trade(d17 + 1_000_000, "B01-A", "E-B01", "markout-d17-a", sport="Soccer"),
    ]

    objects: list[dict[str, Any]] = []
    releases = []
    for day in days:
        release_id = f"{day}__v3ref__fixture"
        releases.append({"release_id": release_id, "date": day})
        l1_path = (
            tmp_path
            / "facts/orderbooks_l1/category=Sports"
            / f"date={day}/part.parquet"
        )
        trade_path = (
            tmp_path
            / "facts/trades/category=Sports"
            / f"date={day}/part.parquet"
        )
        dim_path = tmp_path / f"dim/snapshots/date={day}/markets.csv"
        _write_rows(l1_path, schema=L1_SCHEMA, rows=l1_by_day[day], table="l1")
        _write_rows(
            trade_path,
            schema=TRADE_SCHEMA,
            rows=trades_by_day[day],
            table="trades",
        )
        dim_path.parent.mkdir(parents=True, exist_ok=True)
        markets = sorted({row[5] for row in l1_by_day[day]})
        event_for_market = {row[5]: row[6] for row in l1_by_day[day]}
        dim_path.write_text(
            "ticker,event_ticker,occurrence_datetime,close_time\n"
            + "".join(
                f"{market},{event_for_market[market]},{day}T12:00:00Z,{day}T20:00:00Z\n"
                for market in markets
            )
        )
        for channel, path, count in (
            ("orderbooks_l1", l1_path, len(l1_by_day[day])),
            ("trades", trade_path, len(trades_by_day[day])),
        ):
            objects.append(
                {
                    "kind": "facts",
                    "channel": channel,
                    "date": day,
                    "logical_key": (
                        f"warehouse/facts/{channel}/category=Sports/"
                        f"date={day}/part.parquet"
                    ),
                    "local_path": str(path),
                    "release_id": release_id,
                    "source_version_id": f"exact-{day}-{channel}",
                    "sha256": ("a" if channel == "orderbooks_l1" else "b") * 64,
                    "row_count": count,
                }
            )
        objects.append(
            {
                "kind": "dim_snapshot",
                "channel": None,
                "date": day,
                "logical_key": f"warehouse/dim/snapshots/date={day}/markets.csv",
                "local_path": str(dim_path),
                "release_id": release_id,
                "source_version_id": f"exact-{day}-markets",
                "sha256": "c" * 64,
                "row_count": len(markets),
            }
        )
    return {
        "release_ids": [release["release_id"] for release in releases],
        "release_dates": list(days),
        "releases": releases,
        "objects": objects,
    }


def _summary_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(
        (key, row.get(key))
        for key in (
            "query_id",
            "horizon_us",
            "date",
            "sport",
            "phase",
            "book_state",
            "utc_hour",
        )
        if key in row
    )


def _assert_rows_equal(
    actual: list[dict[str, Any]], expected: list[dict[str, Any]]
) -> None:
    actual = sorted(actual, key=_summary_key)
    expected = sorted(expected, key=_summary_key)
    assert len(actual) == len(expected)
    for got, want in zip(actual, expected):
        assert got.keys() == want.keys()
        for key, value in want.items():
            if isinstance(value, float):
                assert math.isclose(got[key], value, rel_tol=1e-12, abs_tol=1e-12), (
                    key,
                    got,
                    want,
                )
            else:
                assert got[key] == value, (key, got, want)


def test_execute_all_bounded_matches_global_results_and_waterfalls_across_boundaries(
    tmp_path: Path,
) -> None:
    manifest = _end_to_end_manifest(tmp_path / "input")
    global_con = duckdb.connect()
    bounded_con = duckdb.connect()
    try:
        global_capabilities, global_methods = execute_all(global_con, manifest)
        bounded_capabilities, bounded_methods, checkpoint_receipt = execute_all_bounded(
            bounded_con,
            manifest,
            tmp_path / "checkpoints",
            market_buckets=2,
        )
    finally:
        global_con.close()
        bounded_con.close()

    assert bounded_capabilities == global_capabilities
    assert [method["method_id"] for method in bounded_methods] == [
        method["method_id"] for method in global_methods
    ]
    assert checkpoint_receipt["state"] == "COMPLETE"

    for bounded, global_result in zip(bounded_methods, global_methods):
        assert bounded["method_id"] == global_result["method_id"]
        assert bounded["status"] == global_result["status"]
        assert bounded["reason"] == global_result["reason"]
        assert bounded["waterfall"] == global_result["waterfall"]
        _assert_rows_equal(bounded["summary"], global_result["summary"])
        for field in ("incidence", "estimability_evidence"):
            assert bounded.get(field) == global_result.get(field)

    by_id = {method["method_id"]: method for method in bounded_methods}
    b01 = by_id["D3-B01-MARKOUT"]
    for row in b01["summary"]:
        assert (row["root_event_proxies"], row["markets"], row["days"]) == (1, 2, 2)
    one_sided = _row(
        by_id["D3-B02-ONESIDE"]["summary"],
        sport="Tennis",
        phase="PRE_SCHEDULED_START",
        book_state="ONE_SIDED",
    )
    assert one_sided["p50_duration_seconds"] == pytest.approx(2.5)
    assert one_sided["p95_duration_seconds"] == pytest.approx(3.85)
    assert (
        one_sided["root_event_proxies"],
        one_sided["markets"],
        one_sided["days"],
    ) == (1, 4, 2)

    # Date 17 has exactly two linked markets, one in each market hash bucket;
    # a local HAVING count(DISTINCT market)>=2 would miss this event.
    b03 = by_id["D3-B03-XMKT"]
    assert b03["estimability_evidence"]["linked_event_proxy_candidates"] == 3

    tennis_rows = [
        row for row in by_id["D3-B04-RHYTHM"]["summary"] if row["sport"] == "Tennis"
    ]
    assert len(tennis_rows) == 2
    assert {row["active_event_minutes"] for row in tennis_rows} == {1}
    assert {row["trades"] for row in tennis_rows} == {1, 2}
    # If cross-date-id were deduplicated independently per date, the two
    # trade counts above would each be one larger.


def test_execute_all_bounded_resume_is_result_identical_and_reuses_every_partition(
    tmp_path: Path,
) -> None:
    manifest = _end_to_end_manifest(tmp_path / "input")
    checkpoint_root = tmp_path / "checkpoints"
    first_con = duckdb.connect()
    second_con = duckdb.connect()
    try:
        first_capabilities, first_methods, first_receipt = execute_all_bounded(
            first_con, manifest, checkpoint_root, market_buckets=2
        )
        second_capabilities, second_methods, second_receipt = execute_all_bounded(
            second_con, manifest, checkpoint_root, market_buckets=2
        )
    finally:
        first_con.close()
        second_con.close()

    assert second_capabilities == first_capabilities
    assert second_methods == first_methods
    assert sum(stage["written_partitions"] for stage in first_receipt["stages"]) > 0
    assert sum(stage["written_partitions"] for stage in second_receipt["stages"]) == 0
    assert all(
        stage["reused_partitions"] == stage["partition_count"]
        for stage in second_receipt["stages"]
    )
    stage_names = {stage["stage"] for stage in second_receipt["stages"]}
    assert {
        "l1_physical_conservation",
        "trades_by_id_conservation",
        "trades_market_conservation",
    } <= stage_names
    for stage in (
        "l1_physical_conservation",
        "trades_by_id_conservation",
        "trades_market_conservation",
    ):
        for path in (checkpoint_root / stage / "receipts").glob("*.json"):
            metrics = json.loads(path.read_text())["metrics"]
            assert metrics["state"] == "PASS"
            assert metrics["observed_rows"] == metrics["expected_rows"]


def test_l1_physical_conservation_uses_manifest_counts_or_direct_count(tmp_path: Path):
    bad = _end_to_end_manifest(tmp_path / "bad-input")
    l1_objects = [row for row in bad["objects"] if row.get("channel") == "orderbooks_l1"]
    l1_objects[0]["row_count"] += 1
    con = duckdb.connect()
    try:
        with pytest.raises(
            RuntimeError,
            match="l1_physical_vs_exact_sports_source.*observed=.*expected=",
        ):
            execute_all_bounded(con, bad, tmp_path / "bad-checkpoints", market_buckets=2)
    finally:
        con.close()

    direct = _end_to_end_manifest(tmp_path / "direct-input")
    direct_l1 = [
        row for row in direct["objects"] if row.get("channel") == "orderbooks_l1"
    ]
    direct_l1[0]["row_count"] = None
    con = duckdb.connect()
    try:
        execute_all_bounded(
            con, direct, tmp_path / "direct-checkpoints", market_buckets=2
        )
    finally:
        con.close()
    receipts = sorted(
        (tmp_path / "direct-checkpoints/l1_physical_conservation/receipts").glob(
            "date=*.json"
        )
    )
    assert receipts
    assert any(
        json.loads(path.read_text())["metrics"].get("source_basis")
        == "DIRECT_COUNTED_EXACT_SPORTS_SOURCE"
        for path in receipts
    )


def _rewrite_canonical_receipt(path: Path, mutate) -> None:
    receipt = json.loads(path.read_text())
    mutate(receipt)
    path.write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="ascii",
    )


@pytest.mark.parametrize(
    ("stage", "label", "mutate"),
    [
        (
            "b01_observations",
            "b01_partition_payload_vs_horizon_metrics",
            lambda receipt: receipt["metrics"]["horizon_rows"].__setitem__(
                next(iter(receipt["metrics"]["horizon_rows"])),
                next(iter(receipt["metrics"]["horizon_rows"].values())) + 1,
            ),
        ),
        (
            "b04_observations",
            "b04_partition_payload_vs_active_market_minutes",
            lambda receipt: receipt["metrics"].__setitem__(
                "active_market_minutes",
                receipt["metrics"]["active_market_minutes"] + 1,
            ),
        ),
    ],
)
def test_observation_receipt_metric_drift_fails_conservation_before_reuse(
    tmp_path: Path, stage: str, label: str, mutate
) -> None:
    manifest = _end_to_end_manifest(tmp_path / "input")
    checkpoint_root = tmp_path / "checkpoints"
    con = duckdb.connect()
    try:
        execute_all_bounded(con, manifest, checkpoint_root, market_buckets=2)
    finally:
        con.close()
    receipt_path = next((checkpoint_root / stage / "receipts").glob("*.json"))
    _rewrite_canonical_receipt(receipt_path, mutate)
    con = duckdb.connect()
    try:
        with pytest.raises(RuntimeError, match=label):
            execute_all_bounded(con, manifest, checkpoint_root, market_buckets=2)
    finally:
        con.close()
