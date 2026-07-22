#!/usr/bin/env python3
"""Bounded, read-only C1 real-fill kill-test runner.

The runner consumes only checkpoint payloads which have first been validated
by :mod:`c1_checkpoint_reader`.  It never opens the Deep03 writer store and it
contains no exchange client or order-writing code.  The economic result is a
gross, fixed-point markout experiment; fees and net PnL are deliberately
``NOT_ESTIMABLE``.

Production work is bounded by one exact date plus one of sixteen non-null L2
market buckets.  Deep03 used 16 L2 buckets and 32 trade-market buckets, so L2
bucket ``b`` is joined only to trade buckets ``b`` and ``b + 16``.  The 17th
L2 partition is the null-market conservation bucket and must contain zero
episodes.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import os
import resource
import shutil
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date as Date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


try:  # Support both repository imports and direct script execution.
    from tools.research.c1_checkpoint_reader import validate_stage
    from tools.research.c1_fill_kernel import (
        FillTrack,
        PublicTrade,
        VirtualOrder,
        simulate_fill,
    )
except ModuleNotFoundError:  # pragma: no cover - direct W09 entry point
    from c1_checkpoint_reader import validate_stage
    from c1_fill_kernel import (
        FillTrack,
        PublicTrade,
        VirtualOrder,
        simulate_fill,
    )


SCHEMA_PARTITION_RECEIPT = "c1-real-fill-partition-receipt-v1"
SCHEMA_AGGREGATES = "c1-real-fill-aggregates-v1"
SCHEMA_RUN_COMPLETE = "c1-real-fill-run-complete-v1"
CLAIM_TIER = "EXPLORATORY_NON_GATE"
FEE_STATE = "NOT_ESTIMABLE_FAIL_CLOSED"
L2_BUCKET_COUNT = 16
TRADE_BUCKET_COUNT = 32
VALID_REPLAY_CLASSIFICATIONS = {"DELTA_APPLIED", "SNAPSHOT_APPLIED"}
TRACK_PUBLIC_NAME = {
    # The config uses the prose label BINDING_STRICT_THROUGH; the kernel and
    # publisher ABI use the shorter machine value STRICT_THROUGH.
    FillTrack.STRICT_THROUGH: "STRICT_THROUGH",
    FillTrack.QUEUE_PESSIMISTIC: "QUEUE_PESSIMISTIC",
    FillTrack.OPTIMISTIC_AT_TOUCH: "OPTIMISTIC_AT_TOUCH",
}


class C1RunnerError(RuntimeError):
    """A frozen input, clock, allocation, or conservation rule failed."""


@dataclass(frozen=True)
class PartitionInput:
    stage: str
    partition_key: str
    path: Path
    sha256: str
    size_bytes: int
    row_count: int
    receipt_sha256: str


@dataclass(frozen=True)
class PartitionBundle:
    date: str
    bucket: int
    episodes: PartitionInput
    replay: PartitionInput
    trades: tuple[PartitionInput, PartitionInput]


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("ascii")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_json_bytes(value)
    with path.open("xb") as handle:
        handle.write(payload)


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _q(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _path_list(paths: Iterable[Path]) -> str:
    values = sorted(Path(path) for path in paths)
    if not values:
        raise C1RunnerError("empty Parquet path list")
    return "[" + ",".join(_q(path) for path in values) + "]"


def _date_text(value: object) -> str:
    if isinstance(value, Date):
        return value.isoformat()
    return str(value)


def _plain_int(value: object, label: str, *, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise C1RunnerError(f"{label} must be a plain integer")
    if minimum is not None and value < minimum:
        raise C1RunnerError(f"{label} must be >= {minimum}")
    return value


def _load_config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise C1RunnerError(f"frozen config unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise C1RunnerError("frozen config must be an object")
    required = {
        "schema_version",
        "experiment_id",
        "runtime_commit",
        "source_binding",
        "eligible_dates",
        "manifest_sha256",
        "upstream_sha256",
        "order_count_e4",
        "min_depletion_fraction",
        "min_depletion_count_e4",
        "refill_fraction",
        "campaign_horizon_us",
        "activation_state_ttl_us",
        "latency_scenarios",
        "cancel_timer_us",
        "markout_horizon_us",
        "fill_tracks",
        "fee_state",
        "live_order_writes_allowed",
    }
    if set(value) != required:
        raise C1RunnerError("frozen config field set differs from audited v1")
    if value["schema_version"] != "c1-real-fill-spec-v1":
        raise C1RunnerError("unsupported C1 config schema")
    if value["fee_state"] != FEE_STATE or value["live_order_writes_allowed"] is not False:
        raise C1RunnerError("config must fail closed on fees and live order writes")
    dates = value["eligible_dates"]
    if dates != ["2026-07-12", "2026-07-15", "2026-07-17"]:
        raise C1RunnerError("eligible dates differ from the frozen clean-date set")
    if value["fill_tracks"] != [
        "BINDING_STRICT_THROUGH",
        "QUEUE_PESSIMISTIC",
        "OPTIMISTIC_AT_TOUCH",
    ]:
        raise C1RunnerError("fill-track order differs from the frozen specification")
    _plain_int(value["order_count_e4"], "order_count_e4", minimum=1)
    _plain_int(value["campaign_horizon_us"], "campaign_horizon_us", minimum=1)
    _plain_int(value["activation_state_ttl_us"], "activation_state_ttl_us", minimum=1)
    if value["cancel_timer_us"] != [200_000, 300_000]:
        raise C1RunnerError("cancel timers differ from frozen specification")
    if value["markout_horizon_us"] != [100_000, 200_000, 500_000, 1_000_000]:
        raise C1RunnerError("markout horizons differ from frozen specification")
    latency = value["latency_scenarios"]
    if latency != [
        {"id": "FAST", "place_us": 5_000, "cancel_us": 5_000},
        {"id": "PRIMARY", "place_us": 50_000, "cancel_us": 50_000},
        {"id": "STRESS", "place_us": 500_000, "cancel_us": 500_000},
    ]:
        raise C1RunnerError("latency scenarios differ from frozen specification")
    return value


def _partition_key(date: str, bucket: int, *, trade: bool = False) -> str:
    if trade:
        return f"date={date}_bucket={bucket:02d}"
    return f"date={date}_bucket={bucket:03d}"


def _partition_map(
    receipt: Mapping[str, Any], checkpoint_root: Path
) -> dict[str, PartitionInput]:
    stage = str(receipt["stage"])
    result: dict[str, PartitionInput] = {}
    for row in receipt["partitions"]:
        item = PartitionInput(
            stage=stage,
            partition_key=str(row["partition_key"]),
            path=checkpoint_root / str(row["data_path"]),
            sha256=str(row["data_sha256"]),
            size_bytes=int(row["size_bytes"]),
            row_count=int(row["row_count"]),
            receipt_sha256=str(row["receipt_sha256"]),
        )
        result[item.partition_key] = item
    return result


def validate_inputs(
    con: Any, *, checkpoint_root: Path, config_path: Path
) -> tuple[dict[str, Any], list[PartitionBundle], dict[str, Any]]:
    """Validate every frozen stage and construct the exact 48 work units."""
    config = _load_config(config_path)
    validation: dict[str, Any] = {}
    for stage in (
        "l2_availability",
        "l2_replay",
        "l2_episodes",
        "trades_market",
        "dim_market_date",
    ):
        validation[stage] = validate_stage(
            con,
            checkpoint_root=checkpoint_root,
            stage=stage,
            config_path=config_path,
        )
        if validation[stage]["source_binding"] != config["source_binding"]:
            raise C1RunnerError(f"source binding mismatch for {stage}")

    episodes = _partition_map(validation["l2_episodes"], checkpoint_root)
    replay = _partition_map(validation["l2_replay"], checkpoint_root)
    trades = _partition_map(validation["trades_market"], checkpoint_root)
    expected_l2 = {
        _partition_key(day, bucket)
        for day in config["eligible_dates"]
        for bucket in range(L2_BUCKET_COUNT + 1)
    }
    if set(episodes) != expected_l2 or set(replay) != expected_l2:
        raise C1RunnerError("L2 replay/episode partition set is not exact 3 x 17")

    bundles: list[PartitionBundle] = []
    for day in config["eligible_dates"]:
        null_key = _partition_key(day, L2_BUCKET_COUNT)
        if episodes[null_key].row_count != 0:
            raise C1RunnerError(f"null-market episode partition is nonempty: {null_key}")
        for bucket in range(L2_BUCKET_COUNT):
            l2_key = _partition_key(day, bucket)
            trade_keys = (
                _partition_key(day, bucket, trade=True),
                _partition_key(day, bucket + L2_BUCKET_COUNT, trade=True),
            )
            if any(key not in trades for key in trade_keys):
                raise C1RunnerError(
                    f"corresponding trade-market partitions absent: {trade_keys}"
                )
            bundles.append(
                PartitionBundle(
                    date=day,
                    bucket=bucket,
                    episodes=episodes[l2_key],
                    replay=replay[l2_key],
                    trades=(trades[trade_keys[0]], trades[trade_keys[1]]),
                )
            )
    if len(bundles) != 48:
        raise AssertionError("C1 partition plan is not 48 work units")
    availability_partition = validation["l2_availability"]["partitions"]
    if len(availability_partition) != 1:
        raise C1RunnerError("L2 availability stage must contain exactly one partition")
    availability_path = checkpoint_root / availability_partition[0]["data_path"]
    availability_rows = _query_dicts(
        con,
        "SELECT date,state,source_rows,analysis_rows,eligible_for_estimands "
        "FROM read_parquet("
        + _q(availability_path)
        + ",hive_partitioning=false) ORDER BY date",
    )
    if len(availability_rows) != 8:
        raise C1RunnerError("L2 availability ledger must cover all eight exact dates")
    input_receipt = {
        "schema_version": "c1-real-fill-input-receipt-v1",
        "state": "PASS",
        "experiment_id": config["experiment_id"],
        "source_binding": config["source_binding"],
        "runtime_commit": config["runtime_commit"],
        "validated_stages": {
            stage: {
                "manifest_sha256": row["manifest_sha256"],
                "validation_sha256": row["validation_sha256"],
                "partition_count": row["partition_count"],
                "row_count": row["row_count"],
            }
            for stage, row in sorted(validation.items())
        },
        "work_partition_count": len(bundles),
        "null_market_partitions_validated": 3,
        "l2_availability": availability_rows,
        "live_order_writes": 0,
    }
    return config, bundles, input_receipt


def _episode_rows(con: Any, path: Path) -> list[dict[str, Any]]:
    description = [
        "episode_id",
        "date",
        "market_ticker",
        "event_proxy",
        "sport",
        "family",
        "side",
        "depletion_ns",
        "refill_ns",
        "refill_fraction",
        "original_touch_price_e4",
        "pre_touch_qty_e4",
        "removed_e4",
        "depletion_fraction",
        "snapshot_epoch",
    ]
    rows = con.execute(
        "SELECT " + ",".join(description) + " FROM read_parquet("
        + _q(path)
        + ",hive_partitioning=false) "
        "ORDER BY market_ticker,depletion_ns,episode_id"
    ).fetchall()
    return [dict(zip(description, row)) for row in rows]


def suppress_overlaps(
    episodes: Sequence[Mapping[str, Any]],
    *,
    campaign_horizon_us: int,
    min_depletion_count_e4: int = 10_000,
    min_depletion_fraction: str = "0.500000",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Greedily retain one campaign per market in the closed 1s window."""
    accepted: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    last_end_by_market: dict[str, int] = {}
    previous_key: tuple[str, int, str] | None = None
    horizon_ns = campaign_horizon_us * 1_000
    seen_ids: set[str] = set()
    for raw in episodes:
        row = dict(raw)
        market = str(row.get("market_ticker") or "")
        episode_id = str(row.get("episode_id") or "")
        depletion = row.get("depletion_ns")
        side = str(row.get("side") or "").lower()
        if not market or not episode_id or type(depletion) is not int or side not in {"yes", "no"}:
            raise C1RunnerError("episode contains an invalid campaign key")
        if _date_text(row.get("date")) not in {
            "2026-07-12",
            "2026-07-15",
            "2026-07-17",
        }:
            raise C1RunnerError("episode date is outside the frozen estimand")
        quote = row.get("original_touch_price_e4")
        pre_quantity = row.get("pre_touch_qty_e4")
        removed = row.get("removed_e4")
        epoch = row.get("snapshot_epoch")
        if (
            type(quote) is not int
            or not 1 <= quote <= 9_999
            or type(pre_quantity) is not int
            or pre_quantity < min_depletion_count_e4
            or type(removed) is not int
            or removed < min_depletion_count_e4
            or removed > pre_quantity
            or type(epoch) is not int
            or epoch < 1
        ):
            raise C1RunnerError("episode trigger quantities/quote/epoch are invalid")
        if Decimal(removed) / Decimal(pre_quantity) < Decimal(min_depletion_fraction):
            raise C1RunnerError("episode is below the frozen depletion threshold")
        stored_fraction = row.get("depletion_fraction")
        if not isinstance(stored_fraction, (int, float)) or isinstance(stored_fraction, bool):
            raise C1RunnerError("episode depletion_fraction is invalid")
        exact_fraction = removed / pre_quantity
        if abs(float(stored_fraction) - exact_fraction) > 1e-12:
            raise C1RunnerError("episode depletion_fraction contradicts quantities")
        refill_ns = row.get("refill_ns")
        refill_fraction = row.get("refill_fraction")
        if (refill_ns is None) != (refill_fraction is None):
            raise C1RunnerError("episode refill clock/fraction presence contradicts")
        if refill_ns is not None and (
            type(refill_ns) is not int
            or refill_ns < depletion
            or refill_ns > depletion + horizon_ns
        ):
            raise C1RunnerError("episode refill clock is outside its frozen horizon")
        if refill_fraction is not None and (
            not isinstance(refill_fraction, (int, float))
            or isinstance(refill_fraction, bool)
            or not _decimal_at_least(refill_fraction, "0.800000")
        ):
            raise C1RunnerError("episode observed refill is below the frozen threshold")
        key = (market, depletion, episode_id)
        if previous_key is not None and key < previous_key:
            raise C1RunnerError("episode input is not canonically sorted")
        previous_key = key
        if episode_id in seen_ids:
            raise C1RunnerError(f"duplicate episode_id: {episode_id}")
        seen_ids.add(episode_id)
        if depletion <= last_end_by_market.get(market, -1):
            excluded.append(
                {
                    "episode_id": episode_id,
                    "date": _date_text(row["date"]),
                    "market_ticker": market,
                    "reason": "OVERLAP_SUPPRESSED",
                }
            )
            continue
        row["date"] = _date_text(row["date"])
        row["side"] = side
        accepted.append(row)
        last_end_by_market[market] = depletion + horizon_ns
    if len(accepted) + len(excluded) != len(episodes):
        raise AssertionError("episode overlap conservation failed")
    return accepted, excluded


def _decimal_at_least(value: object, threshold: str) -> bool:
    if value is None:
        return False
    try:
        return Decimal(str(value)) >= Decimal(threshold)
    except (InvalidOperation, ValueError):
        return False


VARIANT_COLUMNS = (
    "campaign_id",
    "episode_id",
    "date",
    "market_ticker",
    "event_proxy",
    "sport",
    "family",
    "side",
    "quote_price_e4",
    "depletion_ns",
    "snapshot_epoch",
    "latency_id",
    "place_us",
    "cancel_us",
    "cancel_timer_us",
    "activation_ns",
    "cancel_intent_ns",
    "cancel_effective_ns",
    "refill_group",
)


def expand_variants(
    campaigns: Sequence[Mapping[str, Any]], config: Mapping[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    horizon_ns = int(config["campaign_horizon_us"]) * 1_000
    for campaign in campaigns:
        t0 = int(campaign["depletion_ns"])
        refill_ns = campaign.get("refill_ns")
        for latency in config["latency_scenarios"]:
            for timer_us in config["cancel_timer_us"]:
                by_timer = (
                    type(refill_ns) is int
                    and refill_ns <= t0 + int(timer_us) * 1_000
                    and _decimal_at_least(
                        campaign.get("refill_fraction"), str(config["refill_fraction"])
                    )
                )
                intent = t0 + (horizon_ns if by_timer else int(timer_us) * 1_000)
                campaign_id = (
                    f"{campaign['episode_id']}|{latency['id']}|{int(timer_us)}"
                )
                rows.append(
                    {
                        "campaign_id": campaign_id,
                        "episode_id": campaign["episode_id"],
                        "date": campaign["date"],
                        "market_ticker": campaign["market_ticker"],
                        "event_proxy": str(campaign.get("event_proxy") or ""),
                        "sport": str(campaign.get("sport") or "_UNKNOWN"),
                        "family": str(campaign.get("family") or "_UNKNOWN"),
                        "side": campaign["side"],
                        "quote_price_e4": int(campaign["original_touch_price_e4"]),
                        "depletion_ns": t0,
                        "snapshot_epoch": int(campaign["snapshot_epoch"]),
                        "latency_id": latency["id"],
                        "place_us": int(latency["place_us"]),
                        "cancel_us": int(latency["cancel_us"]),
                        "cancel_timer_us": int(timer_us),
                        "activation_ns": t0 + int(latency["place_us"]) * 1_000,
                        "cancel_intent_ns": intent,
                        "cancel_effective_ns": intent + int(latency["cancel_us"]) * 1_000,
                        "refill_group": (
                            "REFILL_BY_TIMER" if by_timer else "NON_REFILL_OR_CENSORED"
                        ),
                    }
                )
    return rows


def _register_dicts(
    con: Any,
    name: str,
    columns: Sequence[tuple[str, str]],
    rows: Sequence[Mapping[str, Any]],
) -> None:
    con.execute(f"DROP TABLE IF EXISTS {name}")
    con.execute(
        f"CREATE TEMP TABLE {name}("
        + ",".join(f'\"{column}\" {kind}' for column, kind in columns)
        + ")"
    )
    if rows:
        names = [column for column, _kind in columns]
        con.executemany(
            f"INSERT INTO {name} VALUES (" + ",".join("?" for _ in names) + ")",
            [[row.get(column) for column in names] for row in rows],
        )


VARIANT_SQL_COLUMNS = (
    ("campaign_id", "VARCHAR"),
    ("episode_id", "VARCHAR"),
    ("date", "DATE"),
    ("market_ticker", "VARCHAR"),
    ("event_proxy", "VARCHAR"),
    ("sport", "VARCHAR"),
    ("family", "VARCHAR"),
    ("side", "VARCHAR"),
    ("quote_price_e4", "BIGINT"),
    ("depletion_ns", "BIGINT"),
    ("snapshot_epoch", "BIGINT"),
    ("latency_id", "VARCHAR"),
    ("place_us", "BIGINT"),
    ("cancel_us", "BIGINT"),
    ("cancel_timer_us", "BIGINT"),
    ("activation_ns", "BIGINT"),
    ("cancel_intent_ns", "BIGINT"),
    ("cancel_effective_ns", "BIGINT"),
    ("refill_group", "VARCHAR"),
)


def activation_states(
    con: Any,
    variants: Sequence[Mapping[str, Any]],
    replay_path: Path,
    *,
    ttl_us: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Past-only ASOF activation state with exact post-only/queue rules."""
    _register_dicts(con, "c1_variants", VARIANT_SQL_COLUMNS, variants)
    con.execute(
        "CREATE OR REPLACE TEMP VIEW c1_replay_latest AS "
        "SELECT * EXCLUDE(_rn) FROM (SELECT *,row_number() OVER ("
        "PARTITION BY market_ticker,recv_wall_ns ORDER BY recv_mono_ns DESC,"
        "ws_sid DESC,ws_seq DESC) AS _rn FROM read_parquet("
        + _q(replay_path)
        + ",hive_partitioning=false)) q WHERE _rn=1"
    )
    columns = [name for name, _kind in VARIANT_SQL_COLUMNS]
    query = (
        "SELECT "
        + ",".join("v.\"" + name + "\"" for name in columns)
        + ",r.recv_wall_ns AS state_ns,r.recv_mono_ns AS state_mono_ns,"
        "r.ws_sid AS state_ws_sid,r.ws_seq AS state_ws_seq,"
        "r.classification AS state_classification,r.snapshot_epoch AS state_epoch,"
        "r.book_valid AS state_book_valid,r.topology AS state_topology,"
        "r.bid_e4 AS state_bid_e4,r.bid_qty_e4 AS state_bid_qty_e4,"
        "r.ask_e4 AS state_ask_e4,r.ask_qty_e4 AS state_ask_qty_e4 "
        "FROM c1_variants v ASOF LEFT JOIN c1_replay_latest r ON "
        "v.market_ticker=r.market_ticker AND v.activation_ns>=r.recv_wall_ns "
        "ORDER BY v.market_ticker,v.depletion_ns,v.latency_id,v.cancel_timer_us"
    )
    names = list(columns) + [
        "state_ns",
        "state_mono_ns",
        "state_ws_sid",
        "state_ws_seq",
        "state_classification",
        "state_epoch",
        "state_book_valid",
        "state_topology",
        "state_bid_e4",
        "state_bid_qty_e4",
        "state_ask_e4",
        "state_ask_qty_e4",
    ]
    result = [dict(zip(names, row)) for row in con.execute(query).fetchall()]
    eligible: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    ttl_ns = ttl_us * 1_000
    for row in result:
        row["date"] = _date_text(row["date"])
        reason: str | None = None
        state_ns = row.get("state_ns")
        if type(state_ns) is not int:
            reason = "ACTIVATION_STATE_MISSING"
        elif state_ns > int(row["activation_ns"]):
            reason = "ACTIVATION_FUTURE_STATE"
        elif int(row["activation_ns"]) - state_ns > ttl_ns:
            reason = "ACTIVATION_STATE_STALE"
        elif row.get("state_classification") not in VALID_REPLAY_CLASSIFICATIONS:
            reason = "ACTIVATION_SEQUENCE_INVALID"
        elif row.get("state_book_valid") is not True:
            reason = "ACTIVATION_BOOK_INVALID"
        elif row.get("state_topology") != "TWO_SIDED":
            reason = "ACTIVATION_NOT_TWO_SIDED"
        elif row.get("state_epoch") != row.get("snapshot_epoch"):
            reason = "ACTIVATION_EPOCH_MISMATCH"
        bid = row.get("state_bid_e4")
        ask = row.get("state_ask_e4")
        quote = int(row["quote_price_e4"])
        side = str(row["side"])
        if reason is None and (
            type(bid) is not int
            or type(ask) is not int
            or not (0 < bid < ask < 10_000)
        ):
            reason = "ACTIVATION_TOUCH_INVALID"
        if reason is None:
            post_only = quote < ask if side == "yes" else quote < 10_000 - bid
            if not post_only:
                reason = "POST_ONLY_REJECTED"
        if reason is not None:
            excluded.append(
                {
                    "episode_id": row["episode_id"],
                    "campaign_id": row["campaign_id"],
                    "date": row["date"],
                    "market_ticker": row["market_ticker"],
                    "latency_id": row["latency_id"],
                    "cancel_timer_us": row["cancel_timer_us"],
                    "reason": reason,
                }
            )
            continue
        raw_best = bid if side == "yes" else 10_000 - ask
        raw_quantity = (
            row["state_bid_qty_e4"] if side == "yes" else row["state_ask_qty_e4"]
        )
        if type(raw_best) is not int or type(raw_quantity) is not int or raw_quantity < 0:
            raise C1RunnerError("activation raw touch is malformed")
        if raw_best == quote:
            row["queue_status"] = "EXACT_TOUCH"
            row["queue_ahead_e4"] = raw_quantity
        elif raw_best < quote:
            row["queue_status"] = "IMPROVED_NEW_TOUCH"
            row["queue_ahead_e4"] = 0
        else:
            row["queue_status"] = "QUEUE_AHEAD_UNKNOWN"
            row["queue_ahead_e4"] = None
        eligible.append(row)
    if len(eligible) + len(excluded) != len(variants):
        raise AssertionError("activation-state conservation failed")
    return eligible, excluded


def _trade_rows_with_state(
    con: Any,
    trade_paths: Sequence[Path],
    replay_path: Path,
    campaigns: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not campaigns:
        return []
    campaign_dates = {str(row["date"]) for row in campaigns}
    if len(campaign_dates) != 1:
        raise C1RunnerError("trade work unit campaigns must bind one exact date")
    expected_date = next(iter(campaign_dates))
    trade_relation = (
        "read_parquet("
        + _path_list(trade_paths)
        + ",union_by_name=true,hive_partitioning=false)"
    )
    wrong_date = int(
        con.execute(
            f"SELECT count(*) FROM {trade_relation} "
            "WHERE date IS NULL OR cast(date AS VARCHAR)<>?",
            [expected_date],
        ).fetchone()[0]
    )
    if wrong_date:
        raise C1RunnerError(
            f"trade-market partition contains rows outside {expected_date}: {wrong_date}"
        )
    windows: dict[str, tuple[int, int]] = {}
    for row in campaigns:
        market = str(row["market_ticker"])
        lo = int(row["activation_ns"]) // 1_000
        hi = int(row["cancel_effective_ns"]) // 1_000
        old = windows.get(market)
        windows[market] = (lo, hi) if old is None else (min(lo, old[0]), max(hi, old[1]))
    _register_dicts(
        con,
        "c1_market_windows",
        (("market_ticker", "VARCHAR"), ("min_us", "BIGINT"), ("max_us", "BIGINT")),
        [
            {"market_ticker": market, "min_us": bounds[0], "max_us": bounds[1]}
            for market, bounds in sorted(windows.items())
        ],
    )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE c1_candidate_trades AS SELECT "
        "t.date,t.t_us,t.market_ticker,t.event_proxy,t.sport,t.trade_id,"
        "t.yes_price_e4,t.count_e4,lower(t.taker_side) AS taker_side "
        "FROM "
        + trade_relation
        + " t "
        "JOIN c1_market_windows w USING(market_ticker) "
        "WHERE t.t_us BETWEEN w.min_us AND w.max_us"
    )
    duplicate = con.execute(
        "SELECT count(*) FROM (SELECT trade_id FROM c1_candidate_trades "
        "GROUP BY trade_id HAVING count(*)<>1)"
    ).fetchone()[0]
    if int(duplicate):
        raise C1RunnerError(f"candidate trade_id is duplicated: {int(duplicate)}")
    query = (
        "SELECT t.date,t.t_us,t.market_ticker,t.event_proxy,t.sport,t.trade_id,"
        "t.yes_price_e4,t.count_e4,t.taker_side,"
        "r.recv_wall_ns AS state_ns,r.classification AS state_classification,"
        "r.snapshot_epoch AS state_epoch,r.book_valid AS state_book_valid,"
        "r.topology AS state_topology,r.bid_e4 AS state_bid_e4,"
        "r.ask_e4 AS state_ask_e4 "
        "FROM c1_candidate_trades t ASOF LEFT JOIN c1_replay_latest r ON "
        "t.market_ticker=r.market_ticker AND t.t_us*1000>=r.recv_wall_ns "
        "ORDER BY t.market_ticker,t.t_us,t.trade_id"
    )
    names = (
        "date",
        "t_us",
        "market_ticker",
        "event_proxy",
        "sport",
        "trade_id",
        "yes_price_e4",
        "count_e4",
        "taker_side",
        "state_ns",
        "state_classification",
        "state_epoch",
        "state_book_valid",
        "state_topology",
        "state_bid_e4",
        "state_ask_e4",
    )
    rows: list[dict[str, Any]] = []
    previous: tuple[str, int, str] | None = None
    for values in con.execute(query).fetchall():
        row = dict(zip(names, values))
        row["date"] = _date_text(row["date"])
        if row["date"] != expected_date:
            raise C1RunnerError("candidate trade escaped exact-date isolation")
        key = (str(row["market_ticker"]), int(row["t_us"]), str(row["trade_id"]))
        if previous is not None and key < previous:
            raise C1RunnerError("trade rows are not canonically sorted")
        previous = key
        if (
            not row["trade_id"]
            or type(row["t_us"]) is not int
            or type(row["yes_price_e4"]) is not int
            or not 1 <= row["yes_price_e4"] <= 9_999
            or type(row["count_e4"]) is not int
            or row["count_e4"] <= 0
            or row["taker_side"] not in {"yes", "no"}
        ):
            raise C1RunnerError("candidate trade row is malformed")
        rows.append(row)
    return rows


def _match_kind(campaign: Mapping[str, Any], trade: Mapping[str, Any]) -> str | None:
    side = campaign["side"]
    quote = int(campaign["quote_price_e4"])
    yes_price = int(trade["yes_price_e4"])
    taker = trade["taker_side"]
    if side == "yes":
        if taker != "no":
            return None
        return "strict" if yes_price < quote else ("touch" if yes_price == quote else None)
    yes_touch = 10_000 - quote
    if taker != "yes":
        return None
    return "strict" if yes_price > yes_touch else ("touch" if yes_price == yes_touch else None)


def _trade_state_valid(
    campaign: Mapping[str, Any], trade: Mapping[str, Any], *, ttl_us: int
) -> bool:
    state_ns = trade.get("state_ns")
    trade_ns = int(trade["t_us"]) * 1_000
    return bool(
        type(state_ns) is int
        and 0 <= trade_ns - state_ns <= ttl_us * 1_000
        and trade.get("state_classification") in VALID_REPLAY_CLASSIFICATIONS
        and trade.get("state_book_valid") is True
        and trade.get("state_topology") == "TWO_SIDED"
        and trade.get("state_epoch") == campaign.get("state_epoch")
    )


def allocate_and_fill(
    campaigns: Sequence[Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Allocate each trade ID to the earliest campaign per track/variant."""
    by_market: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    times_by_market: dict[str, list[int]] = defaultdict(list)
    for trade in trades:
        market = str(trade["market_ticker"])
        by_market[market].append(trade)
        times_by_market[market].append(int(trade["t_us"]))
    by_variant_market: dict[tuple[str, int, str], list[Mapping[str, Any]]] = defaultdict(list)
    for campaign in campaigns:
        by_variant_market[
            (
                str(campaign["latency_id"]),
                int(campaign["cancel_timer_us"]),
                str(campaign["market_ticker"]),
            )
        ].append(campaign)

    slices: list[dict[str, Any]] = []
    allocated_ids: dict[tuple[str, int, str], set[str]] = defaultdict(set)
    public_volume_by_allocation: dict[tuple[str, int, str, str], int] = {}
    for variant_key in sorted(by_variant_market):
        latency_id, timer_us, market = variant_key
        market_trades = by_market.get(market, [])
        market_times = times_by_market.get(market, [])
        ordered_campaigns = sorted(
            by_variant_market[variant_key],
            key=lambda row: (int(row["depletion_ns"]), str(row["campaign_id"])),
        )
        for track in FillTrack:
            public_track = TRACK_PUBLIC_NAME[track]
            allocation_key = (latency_id, timer_us, public_track)
            allocated = allocated_ids[allocation_key]
            for campaign in ordered_campaigns:
                if track is FillTrack.QUEUE_PESSIMISTIC and campaign.get("queue_ahead_e4") is None:
                    continue
                active_us = int(campaign["activation_ns"]) // 1_000
                cancel_us = int(campaign["cancel_effective_ns"]) // 1_000
                lo = bisect.bisect_right(market_times, active_us)
                hi = bisect.bisect_right(market_times, cancel_us)
                selected: list[Mapping[str, Any]] = []
                selected_public: list[PublicTrade] = []
                for trade in market_trades[lo:hi]:
                    trade_id = str(trade["trade_id"])
                    if trade_id in allocated:
                        continue
                    if not _trade_state_valid(
                        campaign, trade, ttl_us=int(config["activation_state_ttl_us"])
                    ):
                        continue
                    kind = _match_kind(campaign, trade)
                    eligible_kind = kind == "strict" or (
                        kind == "touch" and track is not FillTrack.STRICT_THROUGH
                    )
                    if not eligible_kind:
                        continue
                    allocated.add(trade_id)
                    selected.append(trade)
                    selected_public.append(
                        PublicTrade(
                            timestamp_us=int(trade["t_us"]),
                            yes_price_e4=int(trade["yes_price_e4"]),
                            count_e4=int(trade["count_e4"]),
                            taker_side=str(trade["taker_side"]),
                        )
                    )
                    public_volume_by_allocation[
                        (latency_id, timer_us, public_track, trade_id)
                    ] = int(trade["count_e4"])
                order = VirtualOrder(
                    side=str(campaign["side"]),
                    price_e4=int(campaign["quote_price_e4"]),
                    activation_us=active_us,
                    queue_ahead_e4=int(campaign.get("queue_ahead_e4") or 0),
                    quantity_e4=int(config["order_count_e4"]),
                )
                result = simulate_fill(order, selected_public, track)
                # Once this one-contract order is full it no longer competes
                # for later prints.  Release the suffix so a later campaign
                # can consume it; otherwise an already-dead virtual order
                # would conservatively-but-incorrectly starve all successors.
                if result.remaining_e4 == 0:
                    last_fill_index = result.fill_events[-1].trade_index
                    for unused in selected[last_fill_index + 1 :]:
                        unused_id = str(unused["trade_id"])
                        allocated.remove(unused_id)
                        public_volume_by_allocation.pop(
                            (latency_id, timer_us, public_track, unused_id)
                        )
                for sequence, event in enumerate(result.fill_events):
                    source = selected[event.trade_index]
                    slices.append(
                        {
                            "fill_slice_id": (
                                f"{campaign['campaign_id']}|{public_track}|"
                                f"{source['trade_id']}|{sequence}"
                            ),
                            "campaign_id": campaign["campaign_id"],
                            "episode_id": campaign["episode_id"],
                            "date": campaign["date"],
                            "market_ticker": market,
                            "event_proxy": campaign["event_proxy"],
                            "sport": campaign["sport"],
                            "family": campaign["family"],
                            "side": campaign["side"],
                            "quote_price_e4": campaign["quote_price_e4"],
                            "snapshot_epoch": campaign["state_epoch"],
                            "latency_id": latency_id,
                            "cancel_timer_us": timer_us,
                            "track": public_track,
                            "refill_group": campaign["refill_group"],
                            "trade_id": source["trade_id"],
                            "trade_us": int(source["t_us"]),
                            "public_count_e4": int(source["count_e4"]),
                            "fill_count_e4": int(event.fill_count_e4),
                            "fill_reason": event.reason,
                            "queue_before_e4": event.queue_before_e4,
                            "queue_after_e4": event.queue_after_e4,
                        }
                    )
    # Global allocation and volume invariants.
    allocation_rows = sum(len(ids) for ids in allocated_ids.values())
    if allocation_rows != len(public_volume_by_allocation):
        raise C1RunnerError("trade allocation identity conservation failed")
    filled_by_allocation: Counter[tuple[str, int, str, str]] = Counter()
    for row in slices:
        key = (
            row["latency_id"],
            row["cancel_timer_us"],
            row["track"],
            row["trade_id"],
        )
        filled_by_allocation[key] += int(row["fill_count_e4"])
    for key, quantity in filled_by_allocation.items():
        if quantity > public_volume_by_allocation[key]:
            raise C1RunnerError("fill quantity exceeded allocated public volume")
    return slices, {
        "allocated_trade_ids": allocation_rows,
        "fill_slices": len(slices),
        "filled_count_e4": sum(int(row["fill_count_e4"]) for row in slices),
        "duplicate_allocation_count": 0,
        "public_volume_exceeded_count": 0,
    }


CAMPAIGN_COLUMNS = VARIANT_SQL_COLUMNS + (
    ("state_ns", "BIGINT"),
    ("state_epoch", "BIGINT"),
    ("queue_status", "VARCHAR"),
    ("queue_ahead_e4", "BIGINT"),
)


FILL_COLUMNS = (
    ("fill_slice_id", "VARCHAR"),
    ("campaign_id", "VARCHAR"),
    ("episode_id", "VARCHAR"),
    ("date", "DATE"),
    ("market_ticker", "VARCHAR"),
    ("event_proxy", "VARCHAR"),
    ("sport", "VARCHAR"),
    ("family", "VARCHAR"),
    ("side", "VARCHAR"),
    ("quote_price_e4", "BIGINT"),
    ("snapshot_epoch", "BIGINT"),
    ("latency_id", "VARCHAR"),
    ("cancel_timer_us", "BIGINT"),
    ("track", "VARCHAR"),
    ("refill_group", "VARCHAR"),
    ("trade_id", "VARCHAR"),
    ("trade_us", "BIGINT"),
    ("public_count_e4", "BIGINT"),
    ("fill_count_e4", "BIGINT"),
    ("fill_reason", "VARCHAR"),
    ("queue_before_e4", "BIGINT"),
    ("queue_after_e4", "BIGINT"),
)

MARKOUT_COLUMNS = FILL_COLUMNS + (
    ("horizon_us", "BIGINT"),
    ("target_ns", "BIGINT"),
    ("state_ns", "BIGINT"),
    ("state_epoch", "BIGINT"),
    ("markout_status", "VARCHAR"),
    ("exit_price_e4", "BIGINT"),
    ("exit_top_qty_e4", "BIGINT"),
    ("observed_count_e4", "BIGINT"),
    ("censored_count_e4", "BIGINT"),
    ("gross_e4", "BIGINT"),
    ("mid_twice_gross_e4", "BIGINT"),
)


def compute_markouts(
    con: Any,
    slices: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Measure only executable future top quantity, censoring the remainder.

    The frozen partial-liquidity policy is registered before production use:
    at each horizon, at most ``min(fill_count, same_outcome_top_quantity)`` is
    observed.  Every other filled unit remains censored and later receives
    the complete legal outcome-price bound in aggregation.
    """
    requests: list[dict[str, Any]] = []
    for fill in slices:
        for horizon_us in config["markout_horizon_us"]:
            row = dict(fill)
            row["horizon_us"] = int(horizon_us)
            row["target_ns"] = int(fill["trade_us"]) * 1_000 + int(horizon_us) * 1_000
            requests.append(row)
    request_columns = FILL_COLUMNS + (("horizon_us", "BIGINT"), ("target_ns", "BIGINT"))
    _register_dicts(con, "c1_markout_requests", request_columns, requests)
    names = [name for name, _kind in request_columns]
    query = (
        "SELECT "
        + ",".join("m.\"" + name + "\"" for name in names)
        + ",r.recv_wall_ns AS state_ns,r.classification AS state_classification,"
        "r.snapshot_epoch AS state_epoch,r.book_valid AS state_book_valid,"
        "r.topology AS state_topology,r.bid_e4 AS state_bid_e4,"
        "r.bid_qty_e4 AS state_bid_qty_e4,r.ask_e4 AS state_ask_e4,"
        "r.ask_qty_e4 AS state_ask_qty_e4 "
        "FROM c1_markout_requests m ASOF LEFT JOIN c1_replay_latest r ON "
        "m.market_ticker=r.market_ticker AND m.target_ns>=r.recv_wall_ns "
        "ORDER BY m.fill_slice_id,m.horizon_us"
    )
    result_names = names + [
        "state_ns",
        "state_classification",
        "state_epoch",
        "state_book_valid",
        "state_topology",
        "state_bid_e4",
        "state_bid_qty_e4",
        "state_ask_e4",
        "state_ask_qty_e4",
    ]
    ttl_ns = int(config["activation_state_ttl_us"]) * 1_000
    result: list[dict[str, Any]] = []
    for values in con.execute(query).fetchall():
        row = dict(zip(result_names, values))
        row["date"] = _date_text(row["date"])
        state_ns = row.get("state_ns")
        status = "OBSERVED"
        if type(state_ns) is not int:
            status = "CENSORED_MISSING_STATE"
        elif state_ns > int(row["target_ns"]):
            status = "CENSORED_FUTURE_STATE"
        elif int(row["target_ns"]) - state_ns > ttl_ns:
            status = "CENSORED_STALE_STATE"
        elif state_ns // 1_000 <= int(row["trade_us"]):
            # A state no later than the fill print is not future markout
            # evidence.  The trade tape has only microsecond precision, so
            # every L2 state in that complete microsecond is resolved against
            # the strategy, including a recv_wall_ns value numerically above
            # the lower microsecond boundary.
            status = "CENSORED_NO_POST_TRADE_STATE"
        elif row.get("state_classification") not in VALID_REPLAY_CLASSIFICATIONS:
            status = "CENSORED_SEQUENCE_INVALID"
        elif row.get("state_book_valid") is not True or row.get("state_topology") != "TWO_SIDED":
            status = "CENSORED_BOOK_INVALID"
        elif row.get("state_epoch") != row.get("snapshot_epoch"):
            status = "CENSORED_EPOCH_MISMATCH"
        bid = row.get("state_bid_e4")
        ask = row.get("state_ask_e4")
        bid_quantity = row.get("state_bid_qty_e4")
        ask_quantity = row.get("state_ask_qty_e4")
        if status == "OBSERVED" and (
            type(bid) is not int or type(ask) is not int or not (0 < bid < ask < 10_000)
        ):
            status = "CENSORED_TOUCH_INVALID"
        output = {name: row.get(name) for name, _kind in request_columns}
        output["state_ns"] = state_ns
        output["state_epoch"] = row.get("state_epoch")
        output["exit_price_e4"] = None
        output["exit_top_qty_e4"] = None
        output["observed_count_e4"] = 0
        output["censored_count_e4"] = int(row["fill_count_e4"])
        output["gross_e4"] = None
        output["mid_twice_gross_e4"] = None
        if status == "OBSERVED":
            entry = int(row["quote_price_e4"])
            if row["side"] == "yes":
                exit_price = int(bid)
                mid_twice = int(bid) + int(ask)
                exit_quantity = bid_quantity
            else:
                exit_price = 10_000 - int(ask)
                mid_twice = 20_000 - int(bid) - int(ask)
                exit_quantity = ask_quantity
            if type(exit_quantity) is not int or exit_quantity <= 0:
                status = "CENSORED_EXIT_TOP_QTY_INVALID"
            else:
                filled = int(row["fill_count_e4"])
                observed = min(filled, exit_quantity)
                censored = filled - observed
                output["exit_top_qty_e4"] = exit_quantity
                output["exit_price_e4"] = exit_price
                output["observed_count_e4"] = observed
                output["censored_count_e4"] = censored
                output["gross_e4"] = exit_price - entry
                output["mid_twice_gross_e4"] = mid_twice - 2 * entry
                status = (
                    "OBSERVED_FULL"
                    if censored == 0
                    else "OBSERVED_PARTIAL_TOP_DEPTH"
                )
        output["markout_status"] = status
        if (
            int(output["observed_count_e4"])
            + int(output["censored_count_e4"])
            != int(row["fill_count_e4"])
        ):
            raise C1RunnerError("markout executable-quantity conservation failed")
        result.append(output)
    if len(result) != len(requests):
        raise C1RunnerError("markout request conservation failed")
    return result


def _copy_parquet(con: Any, table: str, path: Path, order_by: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(
        f"COPY (SELECT * FROM {table} ORDER BY {order_by}) TO {_q(path)} "
        "(FORMAT PARQUET,COMPRESSION ZSTD)"
    )


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column) for column in columns})


def process_partition(
    con: Any,
    bundle: PartitionBundle,
    config: Mapping[str, Any],
    output_root: Path,
) -> dict[str, Any]:
    episodes = _episode_rows(con, bundle.episodes.path)
    if any(str(row.get("date")) != bundle.date for row in episodes):
        raise C1RunnerError(
            f"episode partition contains a row outside {bundle.date}"
        )
    wrong_replay_date = int(
        con.execute(
            "SELECT count(*) FROM read_parquet("
            + _q(bundle.replay.path)
            + ",hive_partitioning=false) "
            "WHERE date IS NULL OR cast(date AS VARCHAR)<>?",
            [bundle.date],
        ).fetchone()[0]
    )
    if wrong_replay_date:
        raise C1RunnerError(
            f"replay partition contains rows outside {bundle.date}: "
            f"{wrong_replay_date}"
        )
    accepted, overlap_exclusions = suppress_overlaps(
        episodes,
        campaign_horizon_us=int(config["campaign_horizon_us"]),
        min_depletion_count_e4=int(config["min_depletion_count_e4"]),
        min_depletion_fraction=str(config["min_depletion_fraction"]),
    )
    variants = expand_variants(accepted, config)
    eligible, activation_exclusions = activation_states(
        con,
        variants,
        bundle.replay.path,
        ttl_us=int(config["activation_state_ttl_us"]),
    )
    trades = _trade_rows_with_state(
        con,
        [row.path for row in bundle.trades],
        bundle.replay.path,
        eligible,
    )
    fills, queue_qc = allocate_and_fill(eligible, trades, config)
    markouts = compute_markouts(con, fills, config)

    key = f"date={bundle.date}_bucket={bundle.bucket:02d}"
    directory = output_root / "PARTITIONS" / key
    directory.mkdir(parents=True, exist_ok=False)
    _register_dicts(con, "c1_campaign_output", CAMPAIGN_COLUMNS, eligible)
    _register_dicts(con, "c1_fill_output", FILL_COLUMNS, fills)
    _register_dicts(con, "c1_markout_output", MARKOUT_COLUMNS, markouts)
    campaign_path = directory / "CAMPAIGNS.parquet"
    fill_path = directory / "FILL_SLICES.parquet"
    markout_path = directory / "MARKOUTS.parquet"
    exclusion_path = directory / "EXCLUSIONS.csv"
    _copy_parquet(
        con,
        "c1_campaign_output",
        campaign_path,
        "market_ticker,depletion_ns,latency_id,cancel_timer_us,campaign_id",
    )
    _copy_parquet(con, "c1_fill_output", fill_path, "fill_slice_id")
    _copy_parquet(con, "c1_markout_output", markout_path, "fill_slice_id,horizon_us")
    exclusion_rows = overlap_exclusions + activation_exclusions
    _write_csv(
        exclusion_path,
        exclusion_rows,
        (
            "episode_id",
            "campaign_id",
            "date",
            "market_ticker",
            "latency_id",
            "cancel_timer_us",
            "reason",
        ),
    )
    reason_counts = Counter(str(row["reason"]) for row in exclusion_rows)
    receipt = {
        "schema_version": SCHEMA_PARTITION_RECEIPT,
        "state": "COMPLETE",
        "experiment_id": config["experiment_id"],
        "source_binding": config["source_binding"],
        "partition_key": key,
        "date": bundle.date,
        "bucket": bundle.bucket,
        "inputs": {
            row.stage + ":" + row.partition_key: {
                "sha256": row.sha256,
                "size_bytes": row.size_bytes,
                "row_count": row.row_count,
                "receipt_sha256": row.receipt_sha256,
            }
            for row in (bundle.episodes, bundle.replay, *bundle.trades)
        },
        "counts": {
            "episodes": len(episodes),
            "campaigns_accepted": len(accepted),
            "overlap_suppressed": len(overlap_exclusions),
            "variants": len(variants),
            "activation_eligible": len(eligible),
            "activation_excluded": len(activation_exclusions),
            "candidate_trades": len(trades),
            "fill_slices": len(fills),
            "markout_rows": len(markouts),
        },
        "exclusion_reasons": dict(sorted(reason_counts.items())),
        "queue_invariants": queue_qc,
        "clock": {
            "decision_clock": "recv_wall_ns",
            "trade_clock": "t_us_microsecond_receive_clock",
            "past_only_asof": True,
            "activation_ttl_us": config["activation_state_ttl_us"],
            "same_microsecond_resolved_against_strategy": True,
        },
        "markout_policy": {
            "future_state_must_be_after_complete_trade_microsecond": True,
            "executable_quantity": "MIN_FILL_COUNT_AND_SAME_OUTCOME_TOP_QTY",
            "unexecutable_quantity": "CENSORED_WITH_LEGAL_PRICE_BOUNDS",
        },
        "fee_state": FEE_STATE,
        "live_order_writes": 0,
        "artifacts": {
            "campaigns": {
                "path": campaign_path.relative_to(output_root).as_posix(),
                "sha256": _sha256_path(campaign_path),
                "bytes": campaign_path.stat().st_size,
                "rows": len(eligible),
            },
            "fill_slices": {
                "path": fill_path.relative_to(output_root).as_posix(),
                "sha256": _sha256_path(fill_path),
                "bytes": fill_path.stat().st_size,
                "rows": len(fills),
            },
            "markouts": {
                "path": markout_path.relative_to(output_root).as_posix(),
                "sha256": _sha256_path(markout_path),
                "bytes": markout_path.stat().st_size,
                "rows": len(markouts),
            },
            "exclusions": {
                "path": exclusion_path.relative_to(output_root).as_posix(),
                "sha256": _sha256_path(exclusion_path),
                "bytes": exclusion_path.stat().st_size,
                "rows": len(exclusion_rows),
            },
        },
    }
    receipt_path = directory / "PARTITION_RECEIPT.json"
    _write_json(receipt_path, receipt)
    return receipt


def _query_dicts(con: Any, sql: str) -> list[dict[str, Any]]:
    cursor = con.execute(sql)
    names = [row[0] for row in cursor.description]
    rows: list[dict[str, Any]] = []
    for values in cursor.fetchall():
        row = dict(zip(names, values))
        if "date" in row:
            row["date"] = _date_text(row["date"])
        rows.append(row)
    return rows


def _diagnostic_ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _pilot_verdict(
    fill_rates: Sequence[Mapping[str, Any]],
    markouts: Sequence[Mapping[str, Any]],
    eligible_dates: Sequence[str],
) -> dict[str, Any]:
    strict_primary = [
        row
        for row in fill_rates
        if row["track"] == "STRICT_THROUGH"
        and row["latency_id"] == "PRIMARY"
    ]
    total_strict_filled = sum(int(row["filled_count_e4"]) for row in strict_primary)
    if total_strict_filled == 0:
        code = "KILL_C1_ENTRY"
        reason = "strict-through fills are zero under PRIMARY"
        precedence = 2
    else:
        decision = {
            (str(row["date"]), int(row["cancel_timer_us"]), int(row["horizon_us"])): row
            for row in markouts
            if row["track"] == "STRICT_THROUGH"
            and row["latency_id"] == "PRIMARY"
            and int(row["horizon_us"]) in {200_000, 1_000_000}
        }
        cells = [
            decision.get((day, timer, horizon))
            for day in eligible_dates
            for timer in (200_000, 300_000)
            for horizon in (200_000, 1_000_000)
        ]
        all_supported = all(
            row is not None and int(row["total_filled_count_e4"]) > 0
            for row in cells
        )
        # No data-dependent coverage threshold is permitted.  Missing
        # executable quantity receives the complete legal outcome-price
        # interval [0,10000], already folded into these exact weighted bounds.
        all_upper_nonpositive = all_supported and all(
            int(row["weighted_gross_upper_sum_e8"]) <= 0
            for row in cells
            if row is not None
        )
        all_lower_positive = all_supported and all(
            int(row["weighted_gross_lower_sum_e8"]) > 0
            for row in cells
            if row is not None
        )
        if all_upper_nonpositive:
            code = "KILL_C1_ENTRY"
            reason = (
                "even the legal-price upper gross bound is non-positive in every "
                "PRIMARY date/timer cell at 200ms and 1000ms"
            )
            precedence = 2
        elif all_lower_positive:
            code = "RETAIN_FOR_20_DAY_VALIDATION"
            reason = (
                "even the legal-price lower gross bound is positive in every "
                "PRIMARY date/timer cell; this pilot is not a promotion"
            )
            precedence = 3
        else:
            code = "INDETERMINATE_MORE_CLEAN_DAYS"
            reason = (
                "PRIMARY strict-through legal-price censoring bounds overlap zero, "
                "or a required date/timer cell has no support"
            )
            precedence = 4
    return {
        "code": code,
        "precedence": precedence,
        "reason": reason,
        "binding_track": "STRICT_THROUGH",
        "binding_latency": "PRIMARY",
        "decision_rule": "LEGAL_PRICE_CENSORING_BOUNDS_NO_COVERAGE_THRESHOLD",
        "fees_estimated": False,
        "positive_promotion": False,
        "live_ready": False,
    }


def _concentration_rows(
    con: Any, *, cancel_timers: Sequence[int]
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for timer in cancel_timers:
        for identity, column in (("market", "market_ticker"), ("event", "event_proxy")):
            rows = _query_dicts(
                con,
                "SELECT date,coalesce(nullif("
                + column
                + ",''),'_MISSING') AS identity_value,"
                "sum(fill_count_e4)::BIGINT AS quantity_e4 FROM c1_all_fills "
                "WHERE latency_id='PRIMARY' AND track='STRICT_THROUGH' "
                f"AND cancel_timer_us={int(timer)} GROUP BY date,identity_value "
                "ORDER BY date,identity_value",
            )
            grouped: dict[str, list[int]] = defaultdict(list)
            for row in rows:
                grouped[str(row["date"])].append(int(row["quantity_e4"]))
            for day in sorted(grouped):
                quantities = grouped[day]
                total = sum(quantities)
                output.append(
                    {
                        "date": day,
                        "cancel_timer_us": int(timer),
                        "identity": identity,
                        "unique": len(quantities),
                        "filled_count_e4": total,
                        "max_share": (
                            max(quantities) / total if total else None
                        ),
                        "hhi": (
                            sum((quantity / total) ** 2 for quantity in quantities)
                            if total
                            else None
                        ),
                    }
                )
    return output


def _validate_global_output_invariants(con: Any) -> dict[str, int]:
    """Recheck allocation/quantity laws after all 48 shards are merged."""
    duplicate_slice_ids = int(
        con.execute(
            "SELECT count(*) FROM (SELECT fill_slice_id FROM c1_all_fills "
            "GROUP BY fill_slice_id HAVING count(*)<>1)"
        ).fetchone()[0]
    )
    allocation_failures, volume_failures = con.execute(
        """
        WITH allocated AS (
          SELECT latency_id,cancel_timer_us,track,trade_id,
                 count(*) AS slice_rows,
                 count(DISTINCT campaign_id) AS campaigns,
                 count(DISTINCT public_count_e4) AS public_variants,
                 max(public_count_e4) AS public_count_e4,
                 sum(fill_count_e4) AS filled_count_e4
          FROM c1_all_fills
          GROUP BY latency_id,cancel_timer_us,track,trade_id
        )
        SELECT count(*) FILTER (
                 WHERE slice_rows<>1 OR campaigns<>1 OR public_variants<>1
               ),
               count(*) FILTER (
                 WHERE filled_count_e4<=0
                    OR filled_count_e4>public_count_e4
               )
        FROM allocated
        """
    ).fetchone()
    markout_linkage_failures = int(
        con.execute(
            """
            SELECT count(*) FROM (
              SELECT coalesce(f.fill_slice_id,m.fill_slice_id) AS fill_slice_id
              FROM (SELECT DISTINCT fill_slice_id FROM c1_all_fills) f
              FULL OUTER JOIN (
                SELECT DISTINCT fill_slice_id FROM c1_all_markouts
              ) m USING(fill_slice_id)
              WHERE f.fill_slice_id IS NULL OR m.fill_slice_id IS NULL
            ) q
            """
        ).fetchone()[0]
    )
    markout_identity_failures, markout_quantity_failures = con.execute(
        """
        WITH by_slice AS (
          SELECT fill_slice_id,count(*) AS rows,
                 count(DISTINCT horizon_us) AS horizons,
                 min(fill_count_e4) AS min_fill,
                 max(fill_count_e4) AS max_fill,
                 min(observed_count_e4+censored_count_e4) AS min_accounted,
                 max(observed_count_e4+censored_count_e4) AS max_accounted,
                 count(*) FILTER (
                   WHERE horizon_us NOT IN (100000,200000,500000,1000000)
                      OR target_ns<>trade_us*1000+horizon_us*1000
                      OR state_ns>target_ns
                 ) AS bad_clock_rows,
                 count(*) FILTER (
                   WHERE observed_count_e4<0 OR censored_count_e4<0
                      OR (observed_count_e4>0 AND (
                           gross_e4 IS NULL OR mid_twice_gross_e4 IS NULL
                         ))
                      OR observed_count_e4>coalesce(exit_top_qty_e4,0)
                      OR (markout_status='OBSERVED_FULL' AND (
                           observed_count_e4<=0 OR censored_count_e4<>0
                         ))
                      OR (markout_status='OBSERVED_PARTIAL_TOP_DEPTH' AND (
                           observed_count_e4<=0 OR censored_count_e4<=0
                         ))
                      OR (markout_status LIKE 'CENSORED_%' AND observed_count_e4<>0)
                      OR markout_status NOT IN (
                           'OBSERVED_FULL','OBSERVED_PARTIAL_TOP_DEPTH',
                           'CENSORED_MISSING_STATE','CENSORED_FUTURE_STATE',
                           'CENSORED_STALE_STATE','CENSORED_NO_POST_TRADE_STATE',
                           'CENSORED_SEQUENCE_INVALID','CENSORED_BOOK_INVALID',
                           'CENSORED_EPOCH_MISMATCH','CENSORED_TOUCH_INVALID',
                           'CENSORED_EXIT_TOP_QTY_INVALID'
                         )
                      OR (observed_count_e4>0 AND (
                           state_ns IS NULL OR state_ns//1000<=trade_us
                           OR state_epoch IS NULL OR state_epoch<>snapshot_epoch
                           OR exit_price_e4 IS NULL
                           OR exit_price_e4<>quote_price_e4+gross_e4
                           OR exit_price_e4 NOT BETWEEN 1 AND 9999
                         ))
                 ) AS bad_rows
          FROM c1_all_markouts GROUP BY fill_slice_id
        )
        SELECT count(*) FILTER (WHERE rows<>4 OR horizons<>4),
               count(*) FILTER (
                 WHERE min_fill<>max_fill OR min_accounted<>min_fill
                    OR max_accounted<>max_fill OR bad_rows<>0
                    OR bad_clock_rows<>0
               )
        FROM by_slice
        """
    ).fetchone()
    failures = {
        "duplicate_fill_slice_ids": duplicate_slice_ids,
        "cross_partition_trade_allocation_failures": int(allocation_failures),
        "cross_partition_public_volume_failures": int(volume_failures),
        "markout_fill_linkage_failures": markout_linkage_failures,
        "markout_identity_failures": int(markout_identity_failures),
        "markout_quantity_conservation_failures": int(markout_quantity_failures),
    }
    if any(failures.values()):
        raise C1RunnerError(f"global merged-output invariant failed: {failures}")
    return failures


def aggregate_outputs(
    con: Any,
    *,
    output_root: Path,
    config: Mapping[str, Any],
    partition_receipts: Sequence[Mapping[str, Any]],
    input_receipt: Mapping[str, Any],
    config_path: Path,
    run_started_monotonic: float,
) -> dict[str, Any]:
    """Merge 48 immutable work units and publish exact integer ledgers."""
    campaign_paths = [
        output_root / row["artifacts"]["campaigns"]["path"]
        for row in partition_receipts
    ]
    fill_paths = [
        output_root / row["artifacts"]["fill_slices"]["path"]
        for row in partition_receipts
    ]
    markout_paths = [
        output_root / row["artifacts"]["markouts"]["path"]
        for row in partition_receipts
    ]
    con.execute(
        "CREATE OR REPLACE TEMP VIEW c1_all_campaigns AS SELECT * FROM read_parquet("
        + _path_list(campaign_paths)
        + ",union_by_name=true,hive_partitioning=false)"
    )
    con.execute(
        "CREATE OR REPLACE TEMP VIEW c1_all_fills AS SELECT * FROM read_parquet("
        + _path_list(fill_paths)
        + ",union_by_name=true,hive_partitioning=false)"
    )
    con.execute(
        "CREATE OR REPLACE TEMP VIEW c1_all_markouts AS SELECT * FROM read_parquet("
        + _path_list(markout_paths)
        + ",union_by_name=true,hive_partitioning=false)"
    )
    global_output_invariants = _validate_global_output_invariants(con)
    result_cells = [
        {
            "date": day,
            "latency_id": latency["id"],
            "cancel_timer_us": int(timer),
            "track": track,
        }
        for day in config["eligible_dates"]
        for latency in config["latency_scenarios"]
        for timer in config["cancel_timer_us"]
        for track in ("STRICT_THROUGH", "QUEUE_PESSIMISTIC", "OPTIMISTIC_AT_TOUCH")
    ]
    _register_dicts(
        con,
        "c1_result_cells",
        (
            ("date", "DATE"),
            ("latency_id", "VARCHAR"),
            ("cancel_timer_us", "BIGINT"),
            ("track", "VARCHAR"),
        ),
        result_cells,
    )
    table_dir = output_root / "TABLES"
    table_dir.mkdir(parents=True, exist_ok=False)
    combined_campaign_path = table_dir / "CAMPAIGNS.parquet"
    combined_fill_path = table_dir / "FILL_SLICES.parquet"
    combined_markout_path = table_dir / "MARKOUTS.parquet"
    _copy_parquet(
        con,
        "c1_all_campaigns",
        combined_campaign_path,
        "date,market_ticker,depletion_ns,latency_id,cancel_timer_us,campaign_id",
    )
    _copy_parquet(con, "c1_all_fills", combined_fill_path, "fill_slice_id")
    _copy_parquet(
        con, "c1_all_markouts", combined_markout_path, "fill_slice_id,horizon_us"
    )

    accepted_by_date: Counter[str] = Counter()
    reason_by_date: Counter[tuple[str, str]] = Counter()
    for receipt in partition_receipts:
        day = str(receipt["date"])
        accepted_by_date[day] += int(receipt["counts"]["campaigns_accepted"])
        for reason, count in receipt["exclusion_reasons"].items():
            reason_by_date[(day, str(reason))] += int(count)

    fill_rates = _query_dicts(
        con,
        """
        WITH eligible AS (
          SELECT c.date,c.latency_id,c.cancel_timer_us,t.track,c.campaign_id
          FROM c1_all_campaigns c CROSS JOIN (
            VALUES ('STRICT_THROUGH'),('QUEUE_PESSIMISTIC'),
                   ('OPTIMISTIC_AT_TOUCH')
          ) t(track)
          WHERE t.track<>'QUEUE_PESSIMISTIC' OR c.queue_ahead_e4 IS NOT NULL
        ), fill_by_order AS (
          SELECT date,latency_id,cancel_timer_us,track,campaign_id,
                 count(*)::BIGINT AS fill_slices,
                 sum(fill_count_e4)::BIGINT AS filled_count_e4
          FROM c1_all_fills
          GROUP BY date,latency_id,cancel_timer_us,track,campaign_id
        ), observed AS (
          SELECT e.date,e.latency_id,e.cancel_timer_us,e.track,
                 count(*)::BIGINT AS campaigns_eligible,
                 count(*) FILTER (WHERE coalesce(f.filled_count_e4,0)>0)::BIGINT
                   AS filled_orders,
                 coalesce(sum(f.fill_slices),0)::BIGINT AS fill_slices,
                 coalesce(sum(f.filled_count_e4),0)::BIGINT AS filled_count_e4
          FROM eligible e LEFT JOIN fill_by_order f
            USING(date,latency_id,cancel_timer_us,track,campaign_id)
          GROUP BY e.date,e.latency_id,e.cancel_timer_us,e.track
        )
        SELECT g.date,g.latency_id,g.cancel_timer_us,g.track,
               coalesce(o.campaigns_eligible,0)::BIGINT AS campaigns_eligible,
               coalesce(o.filled_orders,0)::BIGINT AS filled_orders,
               coalesce(o.fill_slices,0)::BIGINT AS fill_slices,
               coalesce(o.filled_count_e4,0)::BIGINT AS filled_count_e4
        FROM c1_result_cells g LEFT JOIN observed o
          USING(date,latency_id,cancel_timer_us,track)
        ORDER BY g.date,g.latency_id,g.cancel_timer_us,g.track
        """
    )
    queue_band_rows = _query_dicts(
        con,
        """
        WITH cohort AS (
          SELECT c.date,c.latency_id,c.cancel_timer_us,t.track,c.campaign_id
          FROM c1_all_campaigns c CROSS JOIN (
            VALUES ('STRICT_THROUGH'),('QUEUE_PESSIMISTIC'),
                   ('OPTIMISTIC_AT_TOUCH')
          ) t(track)
          WHERE c.queue_ahead_e4 IS NOT NULL
        ), fill_by_order AS (
          SELECT date,latency_id,cancel_timer_us,track,campaign_id,
                 sum(fill_count_e4)::BIGINT AS filled_count_e4
          FROM c1_all_fills
          GROUP BY date,latency_id,cancel_timer_us,track,campaign_id
        ), observed AS (
          SELECT c.date,c.latency_id,c.cancel_timer_us,c.track,
                 count(*)::BIGINT AS cohort_orders,
                 count(*) FILTER (WHERE coalesce(f.filled_count_e4,0)>0)::BIGINT
                   AS filled_orders,
                 coalesce(sum(f.filled_count_e4),0)::BIGINT AS filled_count_e4
          FROM cohort c LEFT JOIN fill_by_order f
            USING(date,latency_id,cancel_timer_us,track,campaign_id)
          GROUP BY c.date,c.latency_id,c.cancel_timer_us,c.track
        )
        SELECT g.date,g.latency_id,g.cancel_timer_us,g.track,
               coalesce(o.cohort_orders,0)::BIGINT AS cohort_orders,
               coalesce(o.filled_orders,0)::BIGINT AS filled_orders,
               coalesce(o.filled_count_e4,0)::BIGINT AS filled_count_e4
        FROM c1_result_cells g LEFT JOIN observed o
          USING(date,latency_id,cancel_timer_us,track)
        ORDER BY g.date,g.latency_id,g.cancel_timer_us,g.track
        """,
    )
    queue_band_by_key = {
        (
            str(row["date"]),
            str(row["latency_id"]),
            int(row["cancel_timer_us"]),
            str(row["track"]),
        ): row
        for row in queue_band_rows
    }
    if len(fill_rates) != 54 or len(queue_band_rows) != 54:
        raise C1RunnerError("fill-rate result grid is not exact 3x3x2x3")
    for row in fill_rates:
        eligible = int(row["campaigns_eligible"])
        row["campaigns_accepted"] = accepted_by_date[str(row["date"])]
        row["offered_count_e4"] = eligible * int(config["order_count_e4"])
        row["fill_rate_order_num"] = int(row["filled_orders"])
        row["fill_rate_order_den"] = eligible
        row["fill_rate_qty_num_e4"] = int(row["filled_count_e4"])
        row["fill_rate_qty_den_e4"] = row["offered_count_e4"]
        row["fill_rate_order"] = _diagnostic_ratio(
            row["fill_rate_order_num"], row["fill_rate_order_den"]
        )
        row["fill_rate_quantity"] = _diagnostic_ratio(
            row["fill_rate_qty_num_e4"], row["fill_rate_qty_den_e4"]
        )
        row["denominator_scope"] = (
            "QUEUE_RECONSTRUCTABLE_ACTIVATIONS"
            if row["track"] == "QUEUE_PESSIMISTIC"
            else "ALL_ACTIVATION_ELIGIBLE"
        )
        comparison = queue_band_by_key.get(
            (
                str(row["date"]),
                str(row["latency_id"]),
                int(row["cancel_timer_us"]),
                str(row["track"]),
            ),
            {},
        )
        row["queue_band_cohort_id"] = "QUEUE_RECONSTRUCTABLE_ACTIVATIONS"
        row["queue_band_fill_rate_order_num"] = int(
            comparison.get("filled_orders", 0)
        )
        row["queue_band_fill_rate_order_den"] = int(
            comparison.get("cohort_orders", 0)
        )
        row["queue_band_fill_rate_qty_num_e4"] = int(
            comparison.get("filled_count_e4", 0)
        )
        row["queue_band_fill_rate_qty_den_e4"] = (
            row["queue_band_fill_rate_order_den"] * int(config["order_count_e4"])
        )

    markouts = _query_dicts(
        con,
        """
        WITH horizons(horizon_us) AS (
          VALUES (100000::BIGINT),(200000::BIGINT),(500000::BIGINT),
                 (1000000::BIGINT)
        ), grid AS (
          SELECT c.*,h.horizon_us FROM c1_result_cells c CROSS JOIN horizons h
        ), observed AS (
          SELECT date,latency_id,cancel_timer_us,track,horizon_us,
                 count(*)::BIGINT AS total_slices,
                 count(*) FILTER (WHERE observed_count_e4>0)::BIGINT
                   AS observed_slices,
                 count(*) FILTER (WHERE censored_count_e4>0)::BIGINT
                   AS censored_slices,
                 coalesce(sum(fill_count_e4),0)::BIGINT
                   AS total_filled_count_e4,
                 coalesce(sum(observed_count_e4),0)::BIGINT
                   AS observed_count_e4,
                 coalesce(sum(censored_count_e4),0)::BIGINT
                   AS censored_count_e4,
                 coalesce(sum(gross_e4*observed_count_e4),0)::BIGINT
                   AS weighted_gross_sum_e8,
                 coalesce(sum(mid_twice_gross_e4*observed_count_e4),0)::BIGINT
                   AS weighted_mid_twice_sum_e8,
                 coalesce(sum(
                   coalesce(gross_e4,0)*observed_count_e4
                   - quote_price_e4*censored_count_e4
                 ),0)::BIGINT AS weighted_gross_lower_sum_e8,
                 coalesce(sum(
                   coalesce(gross_e4,0)*observed_count_e4
                   + (10000-quote_price_e4)*censored_count_e4
                 ),0)::BIGINT AS weighted_gross_upper_sum_e8
          FROM c1_all_markouts
          GROUP BY date,latency_id,cancel_timer_us,track,horizon_us
        )
        SELECT g.date,g.latency_id,g.cancel_timer_us,g.track,g.horizon_us,
               coalesce(o.total_slices,0)::BIGINT AS total_slices,
               coalesce(o.observed_slices,0)::BIGINT AS observed_slices,
               coalesce(o.censored_slices,0)::BIGINT AS censored_slices,
               coalesce(o.total_filled_count_e4,0)::BIGINT
                 AS total_filled_count_e4,
               coalesce(o.observed_count_e4,0)::BIGINT AS observed_count_e4,
               coalesce(o.censored_count_e4,0)::BIGINT AS censored_count_e4,
               coalesce(o.weighted_gross_sum_e8,0)::BIGINT
                 AS weighted_gross_sum_e8,
               coalesce(o.weighted_mid_twice_sum_e8,0)::BIGINT
                 AS weighted_mid_twice_sum_e8,
               coalesce(o.weighted_gross_lower_sum_e8,0)::BIGINT
                 AS weighted_gross_lower_sum_e8,
               coalesce(o.weighted_gross_upper_sum_e8,0)::BIGINT
                 AS weighted_gross_upper_sum_e8
        FROM grid g LEFT JOIN observed o
          USING(date,latency_id,cancel_timer_us,track,horizon_us)
        ORDER BY g.date,g.latency_id,g.cancel_timer_us,g.track,g.horizon_us
        """
    )
    for row in markouts:
        quantity = int(row["observed_count_e4"])
        if (
            int(row["total_filled_count_e4"])
            != quantity + int(row["censored_count_e4"])
            or int(row["weighted_gross_lower_sum_e8"])
            > int(row["weighted_gross_sum_e8"])
            or int(row["weighted_gross_sum_e8"])
            > int(row["weighted_gross_upper_sum_e8"])
        ):
            raise C1RunnerError("aggregate markout quantity/bound conservation failed")
        row["coverage_count_num_e4"] = quantity
        row["coverage_count_den_e4"] = int(row["total_filled_count_e4"])
        row["coverage_fraction_diagnostic"] = _diagnostic_ratio(
            row["coverage_count_num_e4"], row["coverage_count_den_e4"]
        )
        row["mean_gross_e4"] = _diagnostic_ratio(
            int(row["weighted_gross_sum_e8"]), quantity
        )
        row["mean_mid_e4"] = (
            int(row["weighted_mid_twice_sum_e8"]) / (2 * quantity)
            if quantity
            else None
        )
    if len(markouts) != 216:
        raise C1RunnerError("markout result grid is not exact 3x3x2x3x4")

    attribution = _query_dicts(
        con,
        """
        SELECT date,latency_id,cancel_timer_us,track,refill_group,horizon_us,
               count(*)::BIGINT AS total_slices,
               count(*) FILTER (WHERE observed_count_e4>0)::BIGINT
                 AS observed_slices,
               count(*) FILTER (WHERE censored_count_e4>0)::BIGINT
                 AS censored_slices,
               coalesce(sum(fill_count_e4),0)::BIGINT
                 AS total_filled_count_e4,
               coalesce(sum(observed_count_e4),0)::BIGINT
                 AS observed_count_e4,
               coalesce(sum(censored_count_e4),0)::BIGINT
                 AS censored_count_e4,
               coalesce(sum(gross_e4*observed_count_e4),0)::BIGINT
                 AS weighted_gross_sum_e8,
               coalesce(sum(
                 coalesce(gross_e4,0)*observed_count_e4
                 - quote_price_e4*censored_count_e4
               ),0)::BIGINT AS weighted_gross_lower_sum_e8,
               coalesce(sum(
                 coalesce(gross_e4,0)*observed_count_e4
                 + (10000-quote_price_e4)*censored_count_e4
               ),0)::BIGINT AS weighted_gross_upper_sum_e8
        FROM c1_all_markouts
        GROUP BY date,latency_id,cancel_timer_us,track,refill_group,horizon_us
        ORDER BY date,latency_id,cancel_timer_us,track,refill_group,horizon_us
        """
    )
    for row in attribution:
        if (
            int(row["total_filled_count_e4"])
            != int(row["observed_count_e4"]) + int(row["censored_count_e4"])
            or int(row["weighted_gross_lower_sum_e8"])
            > int(row["weighted_gross_sum_e8"])
            or int(row["weighted_gross_sum_e8"])
            > int(row["weighted_gross_upper_sum_e8"])
        ):
            raise C1RunnerError("attribution quantity/bound conservation failed")
        row["coverage_count_num_e4"] = int(row["observed_count_e4"])
        row["coverage_count_den_e4"] = int(row["total_filled_count_e4"])
        row["mean_gross_e4"] = _diagnostic_ratio(
            int(row["weighted_gross_sum_e8"]), int(row["observed_count_e4"])
        )

    support = _query_dicts(
        con,
        """
        SELECT date,count(DISTINCT episode_id)::BIGINT AS episodes,
               count(DISTINCT market_ticker)::BIGINT AS markets,
               count(DISTINCT nullif(event_proxy,''))::BIGINT AS events
        FROM c1_all_campaigns WHERE latency_id='PRIMARY' AND cancel_timer_us=200000
        GROUP BY date ORDER BY date
        """
    )
    concentration_detail = _concentration_rows(
        con, cancel_timers=[int(value) for value in config["cancel_timer_us"]]
    )
    exclusions = [
        {"date": day, "reason": reason, "count": count}
        for (day, reason), count in sorted(reason_by_date.items())
    ]
    for row in input_receipt.get("l2_availability", []):
        if row.get("eligible_for_estimands") is True:
            continue
        state = str(row.get("state") or "UNKNOWN")
        exclusions.append(
            {
                "date": str(row["date"]),
                "reason": "L2_" + state,
                "count": int(row.get("source_rows") or 0),
                "ledger_status": "DATE_EXCLUDED_BEFORE_CAMPAIGN_FORMATION",
            }
        )
    support_by_date = {str(row["date"]): row for row in support}
    market_concentration = {
        str(row["date"]): row
        for row in concentration_detail
        if int(row["cancel_timer_us"]) == 200_000 and row["identity"] == "market"
    }
    concentration = []
    for day in config["eligible_dates"]:
        support_row = support_by_date.get(day, {})
        concentration_row = market_concentration.get(day, {})
        concentration.append(
            {
                "date": day,
                "events": int(support_row.get("events", 0)),
                "markets": int(support_row.get("markets", 0)),
                "max_market_share": concentration_row.get("max_share", 0.0) or 0.0,
                "hhi": concentration_row.get("hhi", 0.0) or 0.0,
            }
        )
    verdict = _pilot_verdict(fill_rates, markouts, config["eligible_dates"])

    chart_tables = {
        "FILL_RATES.csv": fill_rates,
        "MARKOUTS.csv": markouts,
        "ATTRIBUTION.csv": attribution,
        "CONCENTRATION.csv": concentration,
        "SUPPORT.csv": support,
        "EXCLUSIONS.csv": exclusions,
    }
    for filename, rows in chart_tables.items():
        columns = list(rows[0]) if rows else ["state"]
        _write_csv(table_dir / filename, rows, columns)

    queue_invariants = {
        "duplicate_trade_allocation_count": sum(
            int(row["queue_invariants"]["duplicate_allocation_count"])
            for row in partition_receipts
        ),
        "public_volume_exceeded_count": sum(
            int(row["queue_invariants"]["public_volume_exceeded_count"])
            for row in partition_receipts
        ),
        "allocation_scope": "UNIQUE_PER_LATENCY_CANCEL_TIMER_FILL_TRACK",
        "negative_l2_delta_creates_fill": False,
        "strict_at_touch_creates_fill": False,
        "global_merged_output_invariants": global_output_invariants,
        "queue_band_comparison_cohort": "QUEUE_RECONSTRUCTABLE_ACTIVATIONS",
    }
    aggregates = {
        "schema_version": SCHEMA_AGGREGATES,
        "experiment_id": config["experiment_id"],
        "claim_tier": CLAIM_TIER,
        "fee_state": FEE_STATE,
        "verdict": verdict,
        "summary": {
            "eligible_dates": config["eligible_dates"],
            "work_partitions": len(partition_receipts),
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
        "concentration": concentration,
        "concentration_detail": concentration_detail,
        "exclusions": exclusions,
        "support": support,
        "queue_invariants": queue_invariants,
        "clock_qc": {
            "decision_clock": "recv_wall_ns",
            "trade_resolution": "microsecond_ambiguity_against_strategy",
            "past_only_asof": True,
            "state_ttl_us": config["activation_state_ttl_us"],
        },
        "provenance": {
            "runtime_commit": config["runtime_commit"],
            "source_binding": config["source_binding"],
            "input_validation": input_receipt,
            "config_sha256": _sha256_path(config_path),
        },
    }
    aggregates_path = output_root / "C1_AGGREGATES.json"
    _write_json(aggregates_path, aggregates)
    _write_json(
        output_root / "SPEC_RECEIPT.json",
        {
            "schema_version": "c1-real-fill-spec-receipt-v1",
            "state": "PASS",
            "experiment_id": config["experiment_id"],
            "config_sha256": _sha256_path(config_path),
            "frozen_runtime_commit": config["runtime_commit"],
            "claim_tier": CLAIM_TIER,
        },
    )
    _write_json(
        output_root / "CLOCK_RECEIPT.json",
        {"schema_version": "c1-clock-receipt-v1", "state": "PASS", **aggregates["clock_qc"]},
    )
    _write_json(
        output_root / "QUEUE_RECEIPT.json",
        {"schema_version": "c1-queue-receipt-v1", "state": "PASS", **queue_invariants},
    )
    _write_json(
        output_root / "FEE_RECEIPT.json",
        {
            "schema_version": "c1-fee-receipt-v1",
            "state": FEE_STATE,
            "net_pnl_published": False,
            "reason": "historical fee overrides and per-order rounding accumulator are not exact-bound",
        },
    )
    _write_json(
        output_root / "EXCLUSION_WATERFALL.json",
        {
            "schema_version": "c1-exclusion-waterfall-v1",
            "eligible_dates": config["eligible_dates"],
            "rows": exclusions,
            "trigger_conservation_pass": True,
        },
    )
    usage = resource.getrusage(resource.RUSAGE_SELF)
    _write_json(
        output_root / "RESOURCE_RECEIPT.json",
        {
            "schema_version": "c1-resource-receipt-v1",
            "elapsed_seconds_diagnostic": round(time.monotonic() - run_started_monotonic, 6),
            "max_rss_platform_units": int(usage.ru_maxrss),
            "partition_count": len(partition_receipts),
            "bounded_partitioning": "ONE_DATE_X_ONE_OF_16_L2_MARKET_BUCKETS",
        },
    )
    source_paths = {
        "runner": Path(__file__),
        "kernel": Path(__file__).with_name("c1_fill_kernel.py"),
        "checkpoint_reader": Path(__file__).with_name("c1_checkpoint_reader.py"),
        "config": config_path,
    }
    reproduction = {
        "schema_version": "c1-reproduction-receipt-v1",
        "source_sha256": {
            name: _sha256_path(path) for name, path in sorted(source_paths.items())
        },
        "runtime_commit": config["runtime_commit"],
        "source_binding": config["source_binding"],
        "partition_order": [str(row["partition_key"]) for row in partition_receipts],
        "network_reads": 0,
        "live_order_writes": 0,
    }
    _write_json(output_root / "REPRODUCTION_RECEIPT.json", reproduction)

    # The publisher may append HTML/PDF later and refresh this ledger.  This
    # runner ledger binds every analysis artifact available at completion.
    ledger_rows: list[dict[str, Any]] = []
    for path in sorted(output_root.rglob("*")):
        if not path.is_file() or path.name in {"ANALYSIS_ARTIFACT_SHA256.json", "C1_RUN_COMPLETE.json"}:
            continue
        ledger_rows.append(
            {
                "path": path.relative_to(output_root).as_posix(),
                "sha256": _sha256_path(path),
                "bytes": path.stat().st_size,
            }
        )
    ledger = {
        "schema_version": "c1-analysis-artifact-ledger-v1",
        "artifacts": ledger_rows,
    }
    ledger_path = output_root / "ANALYSIS_ARTIFACT_SHA256.json"
    _write_json(ledger_path, ledger)
    complete = {
        "schema_version": SCHEMA_RUN_COMPLETE,
        "state": "COMPLETE",
        "experiment_id": config["experiment_id"],
        "claim_tier": CLAIM_TIER,
        "verdict": verdict,
        "partition_receipts": len(partition_receipts),
        "analysis_artifact_ledger_sha256": _sha256_path(ledger_path),
        "live_order_writes": 0,
        "report_publication_pending": True,
    }
    _write_json(output_root / "C1_RUN_COMPLETE.json", complete)
    return aggregates


def _configure_duckdb(con: Any, output_root: Path) -> None:
    scratch = output_root / "SCRATCH"
    scratch.mkdir(parents=True, exist_ok=True)
    con.execute("SET threads=2")
    con.execute("SET memory_limit='16GB'")
    con.execute("SET preserve_insertion_order=false")
    con.execute("SET temp_directory=" + _q(scratch))


def run(
    *, checkpoint_root: Path, output_dir: Path, config_path: Path
) -> dict[str, Any]:
    """Validate, execute all 48 bounded partitions, and aggregate results."""
    import duckdb

    if output_dir.exists():
        raise C1RunnerError(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True)
    started = time.monotonic()
    con = duckdb.connect()
    try:
        _configure_duckdb(con, output_dir)
        config, bundles, input_receipt = validate_inputs(
            con, checkpoint_root=checkpoint_root, config_path=config_path
        )
        _write_json(output_dir / "INPUT_RECEIPT.json", input_receipt)
        receipts: list[dict[str, Any]] = []
        for bundle in bundles:
            receipt = process_partition(con, bundle, config, output_dir)
            receipts.append(receipt)
            print(
                "C1_PARTITION_COMPLETE "
                f"date={bundle.date} bucket={bundle.bucket:02d} "
                f"fills={receipt['counts']['fill_slices']}",
                flush=True,
            )
        return aggregate_outputs(
            con,
            output_root=output_dir,
            config=config,
            partition_receipts=receipts,
            input_receipt=input_receipt,
            config_path=config_path,
            run_started_monotonic=started,
        )
    except Exception:
        # Preserve partial, immutable partition receipts for diagnosis.  A new
        # run must use a new output directory; no partial artifact is trusted.
        raise
    finally:
        con.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--config",
        required=True,
        type=Path,
        help="externally frozen c1_real_fill_v1.json",
    )
    args = parser.parse_args(argv)
    try:
        aggregates = run(
            checkpoint_root=args.checkpoint_root,
            output_dir=args.output_dir,
            config_path=args.config,
        )
    except Exception as exc:
        print(f"C1_REAL_FILL_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        "C1_REAL_FILL_COMPLETE verdict=" + str(aggregates["verdict"]["code"]),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
