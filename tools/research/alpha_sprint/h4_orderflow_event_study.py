#!/usr/bin/env python3
"""Run the H4 past-only order-flow event study on Deep03 checkpoints.

This runner is intentionally narrow:

* TRAIN is exactly 2026-07-12 and 2026-07-15.
* VALIDATE is exactly 2026-07-17 and is never read while fitting the sport
  universe, quote-velocity scale, or signal threshold.
* Features use only the preceding one second of signed trades and quote
  history plus the currently effective L2 depth imbalance.
* Returns cross the displayed touch at entry and liquidation and are reported
  as effective-time, one-contract *gross* returns.  They are not fills, do not
  include fees or latency, and are not cash PnL.

The implementation reads one L2 market-hash partition at a time and maps it to
the corresponding trade partitions.  DuckDB is fixed at 16 GB and two threads;
intermediate feature rows are Parquet shards so the full corpus is never
collected in Python memory.
"""

from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    import duckdb
except ImportError as exc:  # pragma: no cover - exercised by the W09 wrapper
    raise SystemExit(
        "duckdb is required; on W09 use /opt/w09/venv/bin/python"
    ) from exc


TRAIN_DATES: Tuple[str, ...] = ("2026-07-12", "2026-07-15")
VALIDATE_DATES: Tuple[str, ...] = ("2026-07-17",)
ALL_DATES: Tuple[str, ...] = TRAIN_DATES + VALIDATE_DATES
HORIZONS_US: Tuple[int, ...] = (100_000, 1_000_000, 5_000_000)
LOOKBACK_US = 1_000_000
MAX_OUTCOME_QUOTE_AGE_US = 1_000_000
ONE_CONTRACT_QTY_E4 = 10_000
MEMORY_LIMIT = "16GB"
THREADS = 2
EXPECTED_L2_MARKET_BUCKETS = 16
EXPECTED_TRADE_MARKET_BUCKETS = 32
DEFAULT_SIGNAL_QUANTILE = 0.90
DEFAULT_MIN_TRAIN_CANDIDATES_PER_SPORT = 100
TRAIN_SCREENED_SPORTS: Tuple[str, ...] = (
    "Basketball",
    "Cricket",
    "Esports",
    "Tennis",
)
SPORT_SCREEN_AUTHORITY_RELATIVE = Path(
    "docs/research_reports/"
    "ALPHA_SPRINT_01_H4_EARLY_DIAGNOSTIC_2026-07-24.md"
)
RECEIPT_NAME = "H4_EVENT_STUDY_RECEIPT.json"
REPORT_NAME = "H4_EVENT_STUDY_REPORT.md"
MODEL_SEAL_NAME = "H4_TRAIN_MODEL_SEAL.json"
SCHEMA_VERSION = "alpha-sprint-h4-orderflow-event-study-v1"
FEATURE_SCHEMA_VERSION = "alpha-sprint-h4-past-only-features-v1"
MODEL_SCHEMA_VERSION = "alpha-sprint-h4-train-only-model-v1"
PARTITION_RE = re.compile(
    r"^date=(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})_bucket=(?P<bucket>[0-9]{2,3})$"
)

L2_REQUIRED_COLUMNS = {
    "date",
    "t_us",
    "recv_wall_ns",
    "recv_mono_ns",
    "market_ticker",
    "sport",
    "ws_sid",
    "ws_seq",
    "classification",
    "snapshot_epoch",
    "book_valid",
    "topology",
    "bid_e4",
    "bid_qty_e4",
    "ask_e4",
    "ask_qty_e4",
    "mid_e4",
    "microprice_e4",
    "imbalance_depth3",
    "top_changed",
}
TRADE_REQUIRED_COLUMNS = {
    "date",
    "t_us",
    "market_ticker",
    "count_e4",
    "taker_side",
}


class H4StudyError(RuntimeError):
    """Fail-closed input, split, or output contract error."""


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


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _quote(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _path_list(paths: Sequence[Path]) -> str:
    if not paths:
        raise H4StudyError("empty Parquet path list")
    return "[" + ",".join(_quote(path) for path in paths) + "]"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _rows(con: Any, sql: str) -> List[Dict[str, Any]]:
    cursor = con.execute(sql)
    names = [item[0] for item in cursor.description]
    return [
        {
            name: _jsonable(value)
            for name, value in zip(names, row)
        }
        for row in cursor.fetchall()
    ]


def _module_sha256() -> str:
    return _sha256_path(Path(__file__).resolve())


def _sport_screen_authority() -> Dict[str, Any]:
    repository_root = Path(__file__).resolve().parents[3]
    path = repository_root / SPORT_SCREEN_AUTHORITY_RELATIVE
    if not path.is_file() or path.is_symlink():
        raise H4StudyError(f"TRAIN sport-screen authority is absent: {path}")
    text = path.read_text(encoding="utf-8")
    required_markers = list(TRAIN_DATES) + list(TRAIN_SCREENED_SPORTS)
    missing = [marker for marker in required_markers if marker not in text]
    if missing:
        raise H4StudyError(
            f"TRAIN sport-screen authority is incomplete: missing={missing}"
        )
    return {
        "path": SPORT_SCREEN_AUTHORITY_RELATIVE.as_posix(),
        "sha256": _sha256_path(path),
        "train_dates": list(TRAIN_DATES),
        "admitted_sports": list(TRAIN_SCREENED_SPORTS),
    }


def _parse_partition_key(key: str) -> Tuple[str, int]:
    match = PARTITION_RE.fullmatch(key)
    if match is None:
        raise H4StudyError(f"unexpected checkpoint partition key: {key}")
    return match.group("date"), int(match.group("bucket"))


def _schema_names(receipt: Mapping[str, Any]) -> set:
    data = receipt.get("data")
    if not isinstance(data, Mapping):
        return set()
    schema = data.get("schema")
    if not isinstance(schema, list):
        return set()
    return {
        str(row.get("name"))
        for row in schema
        if isinstance(row, Mapping) and isinstance(row.get("name"), str)
    }


def _load_stage(
    checkpoint_namespace: Path,
    stage: str,
    required_columns: set,
    verify_payload_hashes: bool,
) -> Dict[str, Any]:
    manifest_path = checkpoint_namespace / stage / "MANIFEST.json"
    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or not checkpoint_namespace.is_dir()
    ):
        raise H4StudyError(f"checkpoint stage manifest is unavailable: {manifest_path}")
    raw_manifest = manifest_path.read_bytes()
    try:
        manifest = json.loads(raw_manifest)
    except ValueError as exc:
        raise H4StudyError(f"checkpoint manifest is not JSON: {manifest_path}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("state") != "COMPLETE"
        or manifest.get("stage") != stage
        or not isinstance(manifest.get("source_binding"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", manifest["source_binding"])
    ):
        raise H4StudyError(f"checkpoint manifest binding/state mismatch: {stage}")
    partitions_raw = manifest.get("partitions")
    if not isinstance(partitions_raw, list) or not partitions_raw:
        raise H4StudyError(f"checkpoint stage has no partitions: {stage}")

    partitions: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for item in partitions_raw:
        if not isinstance(item, Mapping):
            raise H4StudyError(f"invalid checkpoint manifest partition: {stage}")
        key = item.get("partition_key")
        receipt_relative = item.get("receipt_path")
        if not isinstance(key, str) or not isinstance(receipt_relative, str):
            raise H4StudyError(f"checkpoint partition identity is absent: {stage}")
        date, bucket = _parse_partition_key(key)
        bucket_text = key.rsplit("=", 1)[-1]
        expected_width = 3 if stage == "l2_replay" else 2
        if len(bucket_text) != expected_width:
            raise H4StudyError(
                f"unexpected checkpoint bucket width: {stage}:{key}:"
                f"expected={expected_width}"
            )
        receipt_path = checkpoint_namespace / receipt_relative
        expected_receipt_path = (
            checkpoint_namespace / stage / "receipts" / f"{key}.json"
        )
        if (
            receipt_path.resolve() != expected_receipt_path.resolve()
            or receipt_path.is_symlink()
            or not receipt_path.is_file()
        ):
            raise H4StudyError(f"checkpoint receipt path mismatch: {stage}:{key}")
        receipt_raw = receipt_path.read_bytes()
        if item.get("receipt_sha256") != _sha256_bytes(receipt_raw):
            raise H4StudyError(f"checkpoint receipt hash mismatch: {stage}:{key}")
        try:
            receipt = json.loads(receipt_raw)
        except ValueError as exc:
            raise H4StudyError(
                f"checkpoint receipt is not JSON: {stage}:{key}"
            ) from exc
        if (
            not isinstance(receipt, dict)
            or receipt.get("state") != "COMPLETE"
            or receipt.get("stage") != stage
            or receipt.get("partition_key") != key
            or receipt.get("source_binding") != manifest["source_binding"]
        ):
            raise H4StudyError(f"checkpoint receipt binding mismatch: {stage}:{key}")
        data = receipt.get("data")
        if not isinstance(data, Mapping) or not isinstance(data.get("path"), str):
            raise H4StudyError(f"checkpoint data receipt is absent: {stage}:{key}")
        data_path = checkpoint_namespace / data["path"]
        expected_data_path = checkpoint_namespace / stage / "data" / f"{key}.parquet"
        if (
            data_path.resolve() != expected_data_path.resolve()
            or data_path.is_symlink()
            or not data_path.is_file()
        ):
            raise H4StudyError(f"checkpoint payload path mismatch: {stage}:{key}")
        if data_path.stat().st_size != data.get("size_bytes"):
            raise H4StudyError(f"checkpoint payload size mismatch: {stage}:{key}")
        if item.get("data_sha256") != data.get("sha256"):
            raise H4StudyError(f"checkpoint payload receipt drift: {stage}:{key}")
        missing = required_columns - _schema_names(receipt)
        if missing:
            raise H4StudyError(
                f"checkpoint schema missing {sorted(missing)}: {stage}:{key}"
            )
        identity = (date, bucket)
        if identity in partitions:
            raise H4StudyError(f"duplicate checkpoint partition: {stage}:{key}")
        partitions[identity] = {
            "key": key,
            "date": date,
            "bucket": bucket,
            "path": data_path,
            "receipt_path": receipt_path,
            "receipt_sha256": item["receipt_sha256"],
            "data_sha256": data["sha256"],
            "verify_payload_hash_when_opened": verify_payload_hashes,
            "row_count": int(data.get("row_count") or 0),
        }

    if len(partitions) != manifest.get("partition_count"):
        raise H4StudyError(f"checkpoint manifest partition count mismatch: {stage}")
    if sum(row["row_count"] for row in partitions.values()) != manifest.get(
        "row_count"
    ):
        raise H4StudyError(f"checkpoint manifest row count mismatch: {stage}")
    return {
        "stage": stage,
        "stage_version": manifest.get("stage_version"),
        "source_binding": manifest["source_binding"],
        "manifest_path": manifest_path,
        "manifest_sha256": _sha256_bytes(raw_manifest),
        "partitions": partitions,
    }


def _selected_partitions(stage: Mapping[str, Any], date: str) -> List[Dict[str, Any]]:
    rows = [
        value
        for (partition_date, _bucket), value in stage["partitions"].items()
        if partition_date == date
    ]
    return sorted(rows, key=lambda row: row["bucket"])


def _preflight_layout(
    l2_stage: Mapping[str, Any], trade_stage: Mapping[str, Any]
) -> Dict[str, Any]:
    if l2_stage["source_binding"] != trade_stage["source_binding"]:
        raise H4StudyError("L2 and trade checkpoint source bindings differ")
    l2_bucket_count: Optional[int] = None
    trade_bucket_count: Optional[int] = None
    l2_by_date: Dict[str, List[Dict[str, Any]]] = {}
    trades_by_date: Dict[str, List[Dict[str, Any]]] = {}
    for date in ALL_DATES:
        l2_rows = _selected_partitions(l2_stage, date)
        trade_rows = _selected_partitions(trade_stage, date)
        if not l2_rows or not trade_rows:
            raise H4StudyError(f"required checkpoint date is absent: {date}")
        l2_buckets = [row["bucket"] for row in l2_rows]
        # L2 replay reserves the final contiguous bucket for NULL market_ticker.
        inferred_l2_count = len(l2_buckets) - 1
        if (
            inferred_l2_count < 1
            or l2_buckets != list(range(inferred_l2_count + 1))
        ):
            raise H4StudyError(f"unexpected L2 bucket layout: {date}:{l2_buckets}")
        inferred_trade_count = len(trade_rows)
        if [row["bucket"] for row in trade_rows] != list(
            range(inferred_trade_count)
        ):
            raise H4StudyError(f"unexpected trade bucket layout: {date}")
        if inferred_trade_count % inferred_l2_count:
            raise H4StudyError(
                f"trade/L2 hash bucket counts are incompatible: {date}"
            )
        if inferred_l2_count != EXPECTED_L2_MARKET_BUCKETS:
            raise H4StudyError(
                f"W09 L2 bucket contract mismatch: {date}:"
                f"expected={EXPECTED_L2_MARKET_BUCKETS}:"
                f"observed={inferred_l2_count}"
            )
        if inferred_trade_count != EXPECTED_TRADE_MARKET_BUCKETS:
            raise H4StudyError(
                f"W09 trade bucket contract mismatch: {date}:"
                f"expected={EXPECTED_TRADE_MARKET_BUCKETS}:"
                f"observed={inferred_trade_count}"
            )
        if l2_bucket_count not in (None, inferred_l2_count):
            raise H4StudyError("L2 bucket count changes across split dates")
        if trade_bucket_count not in (None, inferred_trade_count):
            raise H4StudyError("trade bucket count changes across split dates")
        l2_bucket_count = inferred_l2_count
        trade_bucket_count = inferred_trade_count
        l2_by_date[date] = l2_rows
        trades_by_date[date] = trade_rows
    return {
        "l2_bucket_count": l2_bucket_count,
        "trade_bucket_count": trade_bucket_count,
        "l2_by_date": l2_by_date,
        "trades_by_date": trades_by_date,
    }


def _assert_partition_membership(
    con: Any,
    partition: Mapping[str, Any],
    *,
    expected_date: str,
    stage: str,
    bucket_count: int,
    null_market_bucket: bool = False,
    verify_payload_hash: bool = False,
) -> Dict[str, Any]:
    """Verify date, hash membership, NULL disposition, and receipt row count."""
    path = Path(partition["path"])
    bucket = int(partition["bucket"])
    relation = f"read_parquet({_quote(path)},hive_partitioning=false)"
    if (
        verify_payload_hash
        and _sha256_path(path) != partition["data_sha256"]
    ):
        raise H4StudyError(
            f"checkpoint payload hash mismatch: {stage}:{partition['key']}"
        )
    invalid_trade_expression = (
        "count(*) FILTER (WHERE count_e4 IS NULL "
        "OR cast(count_e4 AS DOUBLE)<=0 "
        "OR taker_side IS NULL "
        "OR lower(cast(taker_side AS VARCHAR)) NOT IN ('yes','no'))"
        if stage == "trades_market"
        else "0::BIGINT"
    )
    row = con.execute(
        f"""
        SELECT count(*) AS source_rows,
               count(*) FILTER (
                 WHERE date IS NULL OR cast(date AS VARCHAR)<>?
               ) AS wrong_date_rows,
               count(*) FILTER (WHERE market_ticker IS NULL)
                 AS null_market_rows,
               count(*) FILTER (
                 WHERE market_ticker IS NOT NULL
                   AND mod(hash(cast(market_ticker AS VARCHAR)),{bucket_count})
                       <>{bucket}
               ) AS wrong_hash_bucket_rows,
               count(*) FILTER (WHERE market_ticker IS NOT NULL)
                 AS non_null_market_rows,
               {invalid_trade_expression} AS invalid_trade_rows
        FROM {relation}
        """,
        [expected_date],
    ).fetchone()
    metrics = {
        "stage": stage,
        "partition_key": partition["key"],
        "source_rows": int(row[0]),
        "wrong_date_rows": int(row[1]),
        "null_market_rows": int(row[2]),
        "wrong_hash_bucket_rows": int(row[3]),
        "non_null_market_rows": int(row[4]),
        "invalid_trade_rows": int(row[5]),
        "expected_bucket_count": bucket_count,
        "null_market_bucket": null_market_bucket,
        "payload_hash_recomputed": verify_payload_hash,
    }
    if metrics["source_rows"] != int(partition["row_count"]):
        raise H4StudyError(
            f"checkpoint partition row-count drift: {stage}:{partition['key']}"
        )
    if metrics["wrong_date_rows"]:
        raise H4StudyError(
            "checkpoint partition date escape: "
            f"{stage}:{expected_date}:rows={metrics['wrong_date_rows']}"
        )
    if null_market_bucket and metrics["non_null_market_rows"]:
        raise H4StudyError(
            "reserved L2 NULL bucket contains a market: "
            f"{partition['key']}:rows={metrics['non_null_market_rows']}"
        )
    if metrics["wrong_hash_bucket_rows"]:
        raise H4StudyError(
            "checkpoint hash partition mismatch: "
            f"{stage}:{partition['key']}:"
            f"rows={metrics['wrong_hash_bucket_rows']}"
        )
    if metrics["invalid_trade_rows"]:
        raise H4StudyError(
            "invalid non-positive/unsigned trade rows: "
            f"{stage}:{partition['key']}:rows={metrics['invalid_trade_rows']}"
        )
    if not null_market_bucket and metrics["null_market_rows"]:
        raise H4StudyError(
            f"market partition contains NULL ticker: {stage}:{partition['key']}"
        )
    return metrics


def _feature_query(l2_path: Path, trade_paths: Sequence[Path], date: str) -> str:
    l2_relation = f"read_parquet({_quote(l2_path)},hive_partitioning=false)"
    trade_relation = (
        f"read_parquet({_quote(trade_paths[0])},hive_partitioning=false)"
        if len(trade_paths) == 1
        else f"read_parquet({_path_list(trade_paths)},union_by_name=true,"
        "hive_partitioning=false)"
    )
    horizon_joins: List[str] = []
    horizon_columns: List[str] = []
    horizon_names: List[str] = []
    for horizon in HORIZONS_US:
        alias = f"f{horizon}"
        horizon_joins.append(
            f"""
            ASOF LEFT JOIN states {alias}
              ON d.market_ticker={alias}.market_ticker
             AND d.t_us+{horizon}>={alias}.t_us
            """
        )
        for field in (
            "t_us",
            "snapshot_epoch",
            "book_valid",
            "topology",
            "bid_e4",
            "bid_qty_e4",
            "ask_e4",
            "ask_qty_e4",
        ):
            output_name = f"h{horizon}_{field}"
            horizon_columns.append(
                f"{alias}.{field} AS {output_name}"
            )
            horizon_names.append(output_name)
    return f"""
    WITH l2_raw AS (
      SELECT cast(date AS VARCHAR) AS date,cast(t_us AS BIGINT) AS t_us,
             cast(recv_wall_ns AS BIGINT) AS recv_wall_ns,
             cast(recv_mono_ns AS BIGINT) AS recv_mono_ns,
             cast(market_ticker AS VARCHAR) AS market_ticker,
             coalesce(cast(sport AS VARCHAR),'_UNKNOWN') AS sport,
             cast(ws_sid AS BIGINT) AS ws_sid,cast(ws_seq AS BIGINT) AS ws_seq,
             cast(classification AS VARCHAR) AS classification,
             cast(snapshot_epoch AS BIGINT) AS snapshot_epoch,
             coalesce(book_valid,false) AS book_valid,
             cast(topology AS VARCHAR) AS topology,
             cast(bid_e4 AS BIGINT) AS bid_e4,
             cast(bid_qty_e4 AS BIGINT) AS bid_qty_e4,
             cast(ask_e4 AS BIGINT) AS ask_e4,
             cast(ask_qty_e4 AS BIGINT) AS ask_qty_e4,
             cast(mid_e4 AS DOUBLE) AS mid_e4,
             cast(microprice_e4 AS DOUBLE) AS microprice_e4,
             cast(imbalance_depth3 AS DOUBLE) AS imbalance_depth3,
             coalesce(top_changed,false) AS top_changed
      FROM {l2_relation}
      WHERE market_ticker IS NOT NULL AND t_us IS NOT NULL
    ), state_ranked AS (
      SELECT *,row_number() OVER (
        PARTITION BY market_ticker,t_us
        ORDER BY recv_wall_ns DESC NULLS LAST,recv_mono_ns DESC NULLS LAST,
                 ws_sid DESC NULLS LAST,ws_seq DESC NULLS LAST
      ) AS state_rank
      FROM l2_raw
    ), states AS (
      SELECT * EXCLUDE(state_rank) FROM state_ranked WHERE state_rank=1
    ), decision_ranked AS (
      SELECT *,row_number() OVER (
        PARTITION BY market_ticker,t_us
        ORDER BY recv_wall_ns DESC NULLS LAST,recv_mono_ns DESC NULLS LAST,
                 ws_sid DESC NULLS LAST,ws_seq DESC NULLS LAST
      ) AS decision_rank
      FROM l2_raw
      WHERE top_changed AND classification='DELTA_APPLIED'
        AND snapshot_epoch IS NOT NULL
    ), decisions AS (
      SELECT * EXCLUDE(decision_rank) FROM decision_ranked
      WHERE decision_rank=1
    ), market_ends AS (
      SELECT market_ticker,max(t_us) AS market_end_us
      FROM states GROUP BY market_ticker
    ), trade_ticks AS (
      SELECT cast(market_ticker AS VARCHAR) AS market_ticker,
             cast(t_us AS BIGINT) AS t_us,
             sum(CASE WHEN lower(cast(taker_side AS VARCHAR))='yes'
                        AND cast(count_e4 AS DOUBLE)>0
                        THEN cast(count_e4 AS DOUBLE)
                      WHEN lower(cast(taker_side AS VARCHAR))='no'
                        AND cast(count_e4 AS DOUBLE)>0
                        THEN -cast(count_e4 AS DOUBLE) ELSE 0 END) AS signed_tick,
             sum(CASE WHEN lower(cast(taker_side AS VARCHAR)) IN ('yes','no')
                       AND cast(count_e4 AS DOUBLE)>0
                      THEN cast(count_e4 AS DOUBLE) ELSE 0 END) AS total_tick
      FROM {trade_relation}
      WHERE market_ticker IS NOT NULL AND t_us IS NOT NULL
      GROUP BY market_ticker,t_us
    ), trade_cum AS (
      SELECT market_ticker,t_us,
             sum(signed_tick) OVER (
               PARTITION BY market_ticker ORDER BY t_us
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
             ) AS signed_cum,
             sum(total_tick) OVER (
               PARTITION BY market_ticker ORDER BY t_us
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
             ) AS total_cum
      FROM trade_ticks
    ), joined AS (
      SELECT d.*,m.market_end_us,
             p.t_us AS past_quote_t_us,p.book_valid AS past_book_valid,
             p.topology AS past_topology,
             coalesce(p.microprice_e4,p.mid_e4) AS past_quote_e4,
             coalesce(now_cum.signed_cum,0)-coalesce(old_cum.signed_cum,0)
               AS signed_flow_e4,
             coalesce(now_cum.total_cum,0)-coalesce(old_cum.total_cum,0)
               AS total_flow_e4,
             {",".join(horizon_columns)}
      FROM decisions d
      JOIN market_ends m USING(market_ticker)
      ASOF LEFT JOIN states p
        ON d.market_ticker=p.market_ticker
       AND d.snapshot_epoch=p.snapshot_epoch
       AND d.t_us-{LOOKBACK_US}>=p.t_us
      ASOF LEFT JOIN trade_cum now_cum
        ON d.market_ticker=now_cum.market_ticker
       AND d.t_us-1>=now_cum.t_us
      ASOF LEFT JOIN trade_cum old_cum
        ON d.market_ticker=old_cum.market_ticker
       AND d.t_us-{LOOKBACK_US}-1>=old_cum.t_us
      {" ".join(horizon_joins)}
    )
    SELECT date,
           CASE WHEN date IN ({",".join(_quote(value) for value in TRAIN_DATES)})
                THEN 'TRAIN'
                WHEN date IN ({",".join(_quote(value) for value in VALIDATE_DATES)})
                THEN 'VALIDATE' ELSE 'DATE_ESCAPE' END AS split,
           market_ticker,sport,t_us AS decision_t_us,market_end_us,
           snapshot_epoch AS entry_snapshot_epoch,
           bid_e4 AS entry_bid_e4,bid_qty_e4 AS entry_bid_qty_e4,
           ask_e4 AS entry_ask_e4,ask_qty_e4 AS entry_ask_qty_e4,
           coalesce(microprice_e4,mid_e4) AS current_quote_e4,
           past_quote_t_us,past_quote_e4,
           coalesce(microprice_e4,mid_e4)-past_quote_e4 AS quote_velocity_e4,
           signed_flow_e4,total_flow_e4,
           CASE WHEN total_flow_e4>0 THEN signed_flow_e4/total_flow_e4
                ELSE 0.0 END AS flow_imbalance,
           imbalance_depth3,
           coalesce((book_valid AND topology='TWO_SIDED'
             AND bid_e4 BETWEEN 1 AND 9999
             AND ask_e4 BETWEEN 1 AND 9999 AND bid_e4<ask_e4
             AND bid_qty_e4>={ONE_CONTRACT_QTY_E4}
             AND ask_qty_e4>={ONE_CONTRACT_QTY_E4}
             AND coalesce(microprice_e4,mid_e4) IS NOT NULL),false)
             AS entry_depth_ok,
           coalesce((past_quote_t_us IS NOT NULL AND past_book_valid
             AND past_topology='TWO_SIDED' AND past_quote_e4 IS NOT NULL),false)
             AS past_quote_ok,
           coalesce(((book_valid AND topology='TWO_SIDED'
             AND bid_e4 BETWEEN 1 AND 9999
             AND ask_e4 BETWEEN 1 AND 9999 AND bid_e4<ask_e4
             AND bid_qty_e4>={ONE_CONTRACT_QTY_E4}
             AND ask_qty_e4>={ONE_CONTRACT_QTY_E4}
             AND coalesce(microprice_e4,mid_e4) IS NOT NULL)
            AND (past_quote_t_us IS NOT NULL AND past_book_valid
             AND past_topology='TWO_SIDED' AND past_quote_e4 IS NOT NULL)
            AND imbalance_depth3 IS NOT NULL),false) AS feature_eligible,
           {",".join(horizon_names)}
    FROM joined
    """


def _materialize_feature_partition(
    con: Any,
    l2_partition: Mapping[str, Any],
    trade_partitions: Sequence[Mapping[str, Any]],
    output_path: Path,
    *,
    l2_bucket_count: int,
    trade_bucket_count: int,
    verify_payload_hashes: bool,
) -> Dict[str, Any]:
    date = str(l2_partition["date"])
    l2_path = Path(l2_partition["path"])
    trade_paths = [Path(row["path"]) for row in trade_partitions]
    membership_qc = [
        _assert_partition_membership(
            con,
            l2_partition,
            expected_date=date,
            stage="l2_replay",
            bucket_count=l2_bucket_count,
            verify_payload_hash=verify_payload_hashes,
        )
    ]
    membership_qc.extend(
        _assert_partition_membership(
            con,
            row,
            expected_date=date,
            stage="trades_market",
            bucket_count=trade_bucket_count,
            verify_payload_hash=verify_payload_hashes,
        )
        for row in trade_partitions
    )
    query = _feature_query(l2_path, trade_paths, date)
    temporary = output_path.with_name(f".{output_path.name}.tmp")
    con.execute(
        f"COPY ({query}) TO {_quote(temporary)} "
        "(FORMAT PARQUET,COMPRESSION ZSTD)"
    )
    row = con.execute(
        f"""
        SELECT count(*) AS raw_decisions,
               count(*) FILTER (WHERE entry_depth_ok) AS entry_depth_ok,
               count(*) FILTER (WHERE NOT entry_depth_ok)
                 AS insufficient_entry_depth,
               count(*) FILTER (WHERE NOT past_quote_ok) AS no_past_quote,
               count(*) FILTER (WHERE feature_eligible) AS feature_eligible,
               count(*) FILTER (WHERE split='DATE_ESCAPE') AS date_escape
        FROM read_parquet({_quote(temporary)},hive_partitioning=false)
        """
    ).fetchone()
    if int(row[5]):
        temporary.unlink(missing_ok=True)
        raise H4StudyError(f"feature shard contains a split date escape: {date}")
    os.replace(temporary, output_path)
    return {
        "date": date,
        "bucket": int(l2_partition["bucket"]),
        "path": output_path,
        "raw_decisions": int(row[0]),
        "entry_depth_ok": int(row[1]),
        "insufficient_entry_depth": int(row[2]),
        "no_past_quote": int(row[3]),
        "feature_eligible": int(row[4]),
        "partition_membership_qc": membership_qc,
    }


def _feature_relation(paths: Sequence[Path]) -> str:
    return (
        f"read_parquet({_quote(paths[0])},hive_partitioning=false)"
        if len(paths) == 1
        else f"read_parquet({_path_list(paths)},union_by_name=true,"
        "hive_partitioning=false)"
    )


def _sport_literals(sports: Sequence[str]) -> str:
    if not sports:
        return "NULL"
    return ",".join(_quote(value) for value in sports)


def _sport_in_predicate(column: str, sports: Sequence[str]) -> str:
    if not sports:
        return "false"
    return f"{column} IN ({_sport_literals(sports)})"


def _fit_train_only_model(
    con: Any,
    feature_paths: Sequence[Path],
    signal_quantile: float,
    min_train_candidates_per_sport: int,
) -> Dict[str, Any]:
    if not 0.50 <= signal_quantile < 1.0:
        raise H4StudyError("signal quantile must be in [0.50,1.0)")
    if min_train_candidates_per_sport < 1:
        raise H4StudyError("minimum train candidates per sport must be positive")
    relation = _feature_relation(feature_paths)
    train_literals = ",".join(_quote(value) for value in TRAIN_DATES)
    screened_sport_literals = _sport_literals(TRAIN_SCREENED_SPORTS)
    sports_rows = con.execute(
        f"""
        SELECT sport,count(*) AS eligible_rows
        FROM {relation}
        WHERE date IN ({train_literals}) AND split='TRAIN'
          AND feature_eligible AND sport IN ({screened_sport_literals})
        GROUP BY sport HAVING count(*)>={int(min_train_candidates_per_sport)}
        ORDER BY sport
        """
    ).fetchall()
    locked_sports = [str(row[0]) for row in sports_rows]
    train_rows_by_sport = {str(row[0]): int(row[1]) for row in sports_rows}
    model: Dict[str, Any] = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "fit_split": "TRAIN_ONLY",
        "train_dates": list(TRAIN_DATES),
        "validation_dates_read_during_fit": [],
        "sport_screen_authority": _sport_screen_authority(),
        "train_screened_candidate_sports": list(TRAIN_SCREENED_SPORTS),
        "sport_lock_rule": (
            "TRAIN-screened candidate allowlist intersect sports with at least "
            f"{min_train_candidates_per_sport} feature-eligible TRAIN decisions"
        ),
        "locked_sports": locked_sports,
        "train_rows_by_locked_sport": train_rows_by_sport,
        "signal_quantile": signal_quantile,
        "score_definition": (
            "flow_imbalance + depth3_imbalance + "
            "clip(quote_velocity_e4/train_q75_abs_quote_velocity_e4,-1,1)"
        ),
        "enabled": False,
        "disabled_reason": None,
        "quote_velocity_scale_e4": None,
        "absolute_score_threshold": None,
        "train_fit_rows": 0,
    }
    if not locked_sports:
        model["disabled_reason"] = "NO_TRAIN_LOCKED_SPORTS"
    else:
        sport_literals = _sport_literals(locked_sports)
        scale_row = con.execute(
            f"""
            SELECT count(*),
                   quantile_cont(abs(quote_velocity_e4),0.75)
            FROM {relation}
            WHERE date IN ({train_literals}) AND split='TRAIN'
              AND feature_eligible AND sport IN ({sport_literals})
            """
        ).fetchone()
        fit_rows = int(scale_row[0])
        raw_scale = float(scale_row[1]) if scale_row[1] is not None else 0.0
        scale = max(1.0, raw_scale)
        model["train_fit_rows"] = fit_rows
        model["quote_velocity_scale_e4"] = scale
        if fit_rows <= 0:
            model["disabled_reason"] = "ZERO_ELIGIBLE_TRAIN_ROWS"
        else:
            score = (
                "(flow_imbalance+imbalance_depth3+"
                f"greatest(-1.0,least(1.0,quote_velocity_e4/{scale})))"
            )
            threshold_row = con.execute(
                f"""
                SELECT quantile_cont(abs({score}),{signal_quantile})
                FROM {relation}
                WHERE date IN ({train_literals}) AND split='TRAIN'
                  AND feature_eligible AND sport IN ({sport_literals})
                """
            ).fetchone()
            threshold = (
                float(threshold_row[0])
                if threshold_row and threshold_row[0] is not None
                else None
            )
            if threshold is None:
                model["disabled_reason"] = "TRAIN_THRESHOLD_NOT_ESTIMABLE"
            else:
                model["absolute_score_threshold"] = threshold
                model["enabled"] = True
    digest_payload = dict(model)
    model["model_sha256"] = _sha256_bytes(_canonical_bytes(digest_payload))
    return model


def _aggregate_shard_sql(path: Path, model: Mapping[str, Any]) -> str:
    locked_sports = list(model.get("locked_sports") or [])
    enabled = bool(model.get("enabled"))
    scale = float(model.get("quote_velocity_scale_e4") or 1.0)
    threshold = float(model.get("absolute_score_threshold") or 0.0)
    sport_filter = _sport_literals(locked_sports)
    score = (
        "(flow_imbalance+imbalance_depth3+"
        f"greatest(-1.0,least(1.0,quote_velocity_e4/{scale})))"
    )
    signal = (
        f"CASE WHEN feature_eligible AND abs({score})>={threshold} "
        f"AND {score}>0 THEN 1 "
        f"WHEN feature_eligible AND abs({score})>={threshold} "
        f"AND {score}<0 THEN -1 END"
        if enabled and locked_sports
        else "NULL::INTEGER"
    )
    horizon_rows: List[str] = []
    for horizon in HORIZONS_US:
        horizon_rows.append(
            f"""
            SELECT *,{horizon}::BIGINT AS horizon_us,
                   h{horizon}_t_us AS outcome_t_us,
                   h{horizon}_snapshot_epoch AS outcome_snapshot_epoch,
                   h{horizon}_book_valid AS outcome_book_valid,
                   h{horizon}_topology AS outcome_topology,
                   h{horizon}_bid_e4 AS outcome_bid_e4,
                   h{horizon}_bid_qty_e4 AS outcome_bid_qty_e4,
                   h{horizon}_ask_e4 AS outcome_ask_e4,
                   h{horizon}_ask_qty_e4 AS outcome_ask_qty_e4
            FROM controls
            """
        )
    return f"""
    WITH base AS (
      SELECT *,{score} AS score,{signal} AS signal_direction
      FROM read_parquet({_quote(path)},hive_partitioning=false)
      WHERE sport IN ({sport_filter})
    ), controls AS (
      SELECT *,'BASELINE' AS control,signal_direction AS direction FROM base
      UNION ALL
      SELECT *,'REVERSE' AS control,-signal_direction AS direction FROM base
      UNION ALL
      SELECT *,'PLACEBO_HASH_DIRECTION' AS control,
             CASE WHEN signal_direction IS NULL THEN NULL
                  WHEN mod(hash(market_ticker,decision_t_us,
                                'H4_PLACEBO_HASH_DIRECTION_V1'),2)=0
                    THEN 1 ELSE -1 END AS direction
      FROM base
    ), horizons AS (
      {" UNION ALL ".join(horizon_rows)}
    ), report_levels AS (
      SELECT *,sport AS report_sport FROM horizons
      UNION ALL
      SELECT *,'_ALL_LOCKED_SPORTS' AS report_sport FROM horizons
    ), classified AS (
      SELECT *,
        (decision_t_us+horizon_us>market_end_us OR outcome_t_us IS NULL)
          AS outside_observation,
        (decision_t_us+horizon_us<=market_end_us AND outcome_t_us IS NOT NULL
          AND decision_t_us+horizon_us-outcome_t_us>
              {MAX_OUTCOME_QUOTE_AGE_US}) AS stale_outcome,
        (decision_t_us+horizon_us<=market_end_us AND outcome_t_us IS NOT NULL
          AND decision_t_us+horizon_us-outcome_t_us<=
              {MAX_OUTCOME_QUOTE_AGE_US}
          AND (NOT coalesce(outcome_book_valid,false)
               OR coalesce(outcome_topology,'')<>'TWO_SIDED'
               OR outcome_snapshot_epoch IS NULL
               OR outcome_snapshot_epoch<>entry_snapshot_epoch
               OR outcome_bid_e4 IS NULL OR outcome_ask_e4 IS NULL
               OR outcome_bid_e4>=outcome_ask_e4)) AS invalid_outcome_book
      FROM report_levels
    ), valued AS (
      SELECT *,
        (direction IS NOT NULL AND NOT outside_observation
          AND NOT stale_outcome AND NOT invalid_outcome_book
          AND ((direction=1 AND coalesce(outcome_bid_qty_e4,0)>=
                              {ONE_CONTRACT_QTY_E4})
            OR (direction=-1 AND coalesce(outcome_ask_qty_e4,0)>=
                               {ONE_CONTRACT_QTY_E4})))
          AS evaluable,
        CASE
          WHEN direction=1 AND NOT outside_observation AND NOT stale_outcome
            AND NOT invalid_outcome_book
            AND coalesce(outcome_bid_qty_e4,0)>={ONE_CONTRACT_QTY_E4}
            THEN cast(outcome_bid_e4-entry_ask_e4 AS DOUBLE)
          WHEN direction=-1 AND NOT outside_observation AND NOT stale_outcome
            AND NOT invalid_outcome_book
            AND coalesce(outcome_ask_qty_e4,0)>={ONE_CONTRACT_QTY_E4}
            THEN cast(entry_bid_e4-outcome_ask_e4 AS DOUBLE)
        END AS gross_return_e4
      FROM classified
    )
    SELECT split,date,report_sport AS sport,control,horizon_us,
           count(*) AS raw_decision_count,
           count(*) FILTER (WHERE feature_eligible) AS feature_eligible_count,
           count(*) FILTER (WHERE direction IS NOT NULL) AS trigger_count,
           count(*) FILTER (
             WHERE direction IS NOT NULL AND outside_observation
           ) AS outside_observation_count,
           count(*) FILTER (
             WHERE direction IS NOT NULL AND NOT outside_observation
               AND stale_outcome
           ) AS stale_outcome_count,
           count(*) FILTER (
             WHERE direction IS NOT NULL AND NOT outside_observation
               AND NOT stale_outcome AND invalid_outcome_book
           ) AS invalid_outcome_book_count,
           count(*) FILTER (
             WHERE direction IS NOT NULL AND NOT outside_observation
               AND NOT stale_outcome AND NOT invalid_outcome_book
               AND NOT ((direction=1 AND coalesce(outcome_bid_qty_e4,0)>=
                          {ONE_CONTRACT_QTY_E4})
                     OR (direction=-1 AND coalesce(outcome_ask_qty_e4,0)>=
                          {ONE_CONTRACT_QTY_E4}))
           ) AS insufficient_exit_depth_count,
           count(*) FILTER (WHERE evaluable) AS evaluable_count,
           coalesce(sum(gross_return_e4) FILTER (WHERE evaluable),0.0)
             AS gross_return_sum_e4,
           count(*) FILTER (WHERE evaluable AND gross_return_e4>0)
             AS positive_return_count
    FROM valued
    GROUP BY split,date,report_sport,control,horizon_us
    ORDER BY split,date,report_sport,control,horizon_us
    """


def _merge_aggregates(rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    keys = (
        "raw_decision_count",
        "feature_eligible_count",
        "trigger_count",
        "outside_observation_count",
        "stale_outcome_count",
        "invalid_outcome_book_count",
        "insufficient_exit_depth_count",
        "evaluable_count",
        "gross_return_sum_e4",
        "positive_return_count",
    )
    merged: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for row in rows:
        identity = (
            row["split"],
            row["date"],
            row["sport"],
            row["control"],
            int(row["horizon_us"]),
        )
        target = merged.setdefault(
            identity,
            {
                "split": identity[0],
                "date": identity[1],
                "sport": identity[2],
                "control": identity[3],
                "horizon_us": identity[4],
                **{key: 0 for key in keys},
            },
        )
        for key in keys:
            target[key] += row.get(key) or 0
    results: List[Dict[str, Any]] = []
    for identity in sorted(merged):
        row = merged[identity]
        eligible = int(row["feature_eligible_count"])
        raw = int(row["raw_decision_count"])
        triggered = int(row["trigger_count"])
        evaluable = int(row["evaluable_count"])
        gross_sum = float(row["gross_return_sum_e4"])
        classified_triggers = sum(
            int(row[key])
            for key in (
                "outside_observation_count",
                "stale_outcome_count",
                "invalid_outcome_book_count",
                "insufficient_exit_depth_count",
                "evaluable_count",
            )
        )
        if classified_triggers != triggered:
            raise H4StudyError(
                "trigger outcome denominator is not conserved: "
                f"{identity}:triggered={triggered}:classified={classified_triggers}"
            )
        row["trigger_outcome_classification_conserved"] = True
        row["zero_trigger"] = triggered == 0
        row["trigger_rate_per_feature_eligible"] = (
            triggered / eligible if eligible else None
        )
        row["trigger_rate_per_raw_decision"] = (
            triggered / raw if raw else None
        )
        row["evaluable_rate_per_trigger"] = (
            evaluable / triggered if triggered else None
        )
        row["mean_executable_gross_return_e4_per_contract"] = (
            gross_sum / evaluable if evaluable else None
        )
        row["mean_executable_gross_return_cents_per_contract"] = (
            gross_sum / evaluable / 100.0 if evaluable else None
        )
        row["positive_return_rate"] = (
            int(row["positive_return_count"]) / evaluable
            if evaluable
            else None
        )
        results.append(_jsonable(row))
    return results


def _complete_result_grid(
    results: Sequence[Mapping[str, Any]], locked_sports: Sequence[str]
) -> List[Dict[str, Any]]:
    """Make zero-trigger and zero-denominator strata machine explicit."""
    indexed = {
        (
            row["split"],
            row["date"],
            row["sport"],
            row["control"],
            int(row["horizon_us"]),
        ): dict(row)
        for row in results
    }
    sports = list(locked_sports) + ["_ALL_LOCKED_SPORTS"]
    controls = ("BASELINE", "REVERSE", "PLACEBO_HASH_DIRECTION")
    for date in ALL_DATES:
        split = "TRAIN" if date in TRAIN_DATES else "VALIDATE"
        for sport in sports:
            for control in controls:
                for horizon in HORIZONS_US:
                    identity = (split, date, sport, control, horizon)
                    if identity in indexed:
                        continue
                    indexed[identity] = {
                        "split": split,
                        "date": date,
                        "sport": sport,
                        "control": control,
                        "horizon_us": horizon,
                        "raw_decision_count": 0,
                        "feature_eligible_count": 0,
                        "trigger_count": 0,
                        "outside_observation_count": 0,
                        "stale_outcome_count": 0,
                        "invalid_outcome_book_count": 0,
                        "insufficient_exit_depth_count": 0,
                        "evaluable_count": 0,
                        "gross_return_sum_e4": 0.0,
                        "positive_return_count": 0,
                        "trigger_outcome_classification_conserved": True,
                        "zero_trigger": True,
                        "trigger_rate_per_feature_eligible": None,
                        "trigger_rate_per_raw_decision": None,
                        "evaluable_rate_per_trigger": None,
                        "mean_executable_gross_return_e4_per_contract": None,
                        "mean_executable_gross_return_cents_per_contract": None,
                        "positive_return_rate": None,
                    }
    return [_jsonable(indexed[key]) for key in sorted(indexed)]


def _input_diagnostics(
    con: Any,
    feature_paths: Sequence[Path],
    feature_shards: Sequence[Mapping[str, Any]],
    locked_sports: Sequence[str],
    partition_membership_qc: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    relation = _feature_relation(feature_paths)
    locked_predicate = _sport_in_predicate("sport", locked_sports)
    date_rows = _rows(
        con,
        f"""
        SELECT split,date,count(*) AS raw_decisions,
               count(*) FILTER (WHERE entry_depth_ok) AS entry_depth_ok,
               count(*) FILTER (WHERE NOT entry_depth_ok)
                 AS insufficient_entry_depth,
               count(*) FILTER (WHERE NOT past_quote_ok) AS no_past_quote,
               count(*) FILTER (WHERE feature_eligible) AS feature_eligible,
               count(*) FILTER (
                 WHERE feature_eligible AND {locked_predicate}
               ) AS feature_eligible_locked_sport,
               count(*) FILTER (
                 WHERE split='VALIDATE' AND
                       NOT ({locked_predicate})
               ) AS validation_rows_outside_locked_sport_universe
        FROM {relation}
        GROUP BY split,date ORDER BY date
        """,
    )
    for row in date_rows:
        if int(row["raw_decisions"]) != (
            int(row["entry_depth_ok"]) + int(row["insufficient_entry_depth"])
        ):
            raise H4StudyError(
                "entry-depth denominator is not conserved: "
                f"{row['date']}"
            )
        row["entry_depth_classification_conserved"] = True
    membership_rows = [dict(row) for row in partition_membership_qc]
    return {
        "by_date": date_rows,
        "feature_shard_count": len(feature_shards),
        "feature_shards_by_date": {
            date: sum(1 for row in feature_shards if row["date"] == date)
            for date in ALL_DATES
        },
        "partition_membership_qc": membership_rows,
        "partition_membership_all_pass": all(
            row["wrong_date_rows"] == 0
            and row["wrong_hash_bucket_rows"] == 0
            and row["invalid_trade_rows"] == 0
            and (
                row["non_null_market_rows"] == 0
                if row["null_market_bucket"]
                else row["null_market_rows"] == 0
            )
            for row in membership_rows
        ),
        "zero_denominator_policy": (
            "all rates/means are null when their explicit denominator is zero; "
            "zero_trigger remains true"
        ),
    }


def _render_markdown(receipt: Mapping[str, Any]) -> str:
    model = receipt["model"]
    results = [
        row
        for row in receipt["results"]
        if row["control"] == "BASELINE"
    ]
    lines = [
        "# H4 order-flow event study",
        "",
        f"- State: `{receipt['state']}`",
        f"- TRAIN: `{', '.join(TRAIN_DATES)}`",
        f"- VALIDATE: `{', '.join(VALIDATE_DATES)}`",
        f"- Model: `{'ENABLED' if model['enabled'] else 'DISABLED'}`",
        f"- TRAIN-locked sports: `{', '.join(model['locked_sports']) or 'NONE'}`",
        (
            "- Threshold: `"
            + (
                f"{model['absolute_score_threshold']:.6f}"
                if model["absolute_score_threshold"] is not None
                else "NOT_ESTIMABLE"
            )
            + "`"
        ),
        "",
        "## Baseline results",
        "",
        (
            "| split | date | sport | horizon | triggers | evaluable | "
            "mean gross (¢/contract) |"
        ),
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for row in results:
        mean = row["mean_executable_gross_return_cents_per_contract"]
        mean_text = "NA" if mean is None else f"{mean:.4f}"
        lines.append(
            f"| {row['split']} | {row['date']} | {row['sport']} | "
            f"{int(row['horizon_us']) / 1_000_000:g}s | "
            f"{row['trigger_count']} | {row['evaluable_count']} | {mean_text} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation boundary",
            "",
            "These are past-only, effective-time displayed-touch event-study returns. "
            "They assume one contract of displayed depth at entry and exit, before "
            "fees and decision/order latency. They are not observed fills, are not "
            "portfolio-additive, and are **not cash PnL**.",
            "",
            "The 100ms and 1s horizons are primary. The 5s horizon is a "
            "sensitivity / displayed-state proxy only; every exit quote, including "
            "the 5s sensitivity, must have been effective within the prior 1s and "
            "must remain in the entry snapshot epoch.",
            "",
            "The source has no authenticated game phase. Every row is treated as "
            "`UNKNOWN_PHASE`; this report makes no live/pre-game claim. The "
            "2026-07-17 slice is held out only for this H4 fit and is not described "
            "as a globally untouched confirmation cohort.",
            "",
            "Reverse and deterministic hash-direction placebo controls, complete "
            "zero-trigger/no-depth denominators, and source lineage are in "
            f"`{RECEIPT_NAME}`.",
            "",
        ]
    )
    return "\n".join(lines)


def run_event_study(
    checkpoint_namespace: Path,
    output_dir: Path,
    *,
    verify_payload_hashes: bool = False,
    keep_scratch: bool = False,
    signal_quantile: float = DEFAULT_SIGNAL_QUANTILE,
    min_train_candidates_per_sport: int = (
        DEFAULT_MIN_TRAIN_CANDIDATES_PER_SPORT
    ),
) -> Dict[str, Any]:
    checkpoint_namespace = Path(checkpoint_namespace).resolve()
    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise H4StudyError(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True)
    scratch = output_dir / ".scratch"
    feature_dir = scratch / "features"
    duckdb_temp = scratch / "duckdb-temp"
    feature_dir.mkdir(parents=True)
    duckdb_temp.mkdir(parents=True)

    l2_stage = _load_stage(
        checkpoint_namespace,
        "l2_replay",
        L2_REQUIRED_COLUMNS,
        verify_payload_hashes,
    )
    trade_stage = _load_stage(
        checkpoint_namespace,
        "trades_market",
        TRADE_REQUIRED_COLUMNS,
        verify_payload_hashes,
    )
    layout = _preflight_layout(l2_stage, trade_stage)

    con = duckdb.connect(str(scratch / "h4.duckdb"))
    con.execute(f"SET memory_limit={_quote(MEMORY_LIMIT)}")
    con.execute(f"SET threads={THREADS}")
    con.execute("SET preserve_insertion_order=false")
    con.execute(f"SET temp_directory={_quote(duckdb_temp)}")
    feature_shards: List[Dict[str, Any]] = []
    partition_membership_qc: List[Dict[str, Any]] = []
    execution_order: List[str] = []
    model_seal_sha256: Optional[str] = None
    try:
        l2_bucket_count = int(layout["l2_bucket_count"])
        trade_bucket_count = int(layout["trade_bucket_count"])

        def materialize_dates(dates: Sequence[str], split: str) -> None:
            execution_order.append(f"MATERIALIZE_{split}_PAYLOAD_START")
            for date in dates:
                trade_rows = layout["trades_by_date"][date]
                l2_rows = layout["l2_by_date"][date]
                null_partition = next(
                    row
                    for row in l2_rows
                    if int(row["bucket"]) == l2_bucket_count
                )
                partition_membership_qc.append(
                    _assert_partition_membership(
                        con,
                        null_partition,
                        expected_date=date,
                        stage="l2_replay",
                        bucket_count=l2_bucket_count,
                        null_market_bucket=True,
                        verify_payload_hash=verify_payload_hashes,
                    )
                )
                for l2_row in l2_rows:
                    l2_bucket = int(l2_row["bucket"])
                    if l2_bucket == l2_bucket_count:
                        continue
                    mapped_trades = [
                        row
                        for row in trade_rows
                        if int(row["bucket"]) % l2_bucket_count == l2_bucket
                    ]
                    if not mapped_trades:
                        raise H4StudyError(
                            "no trade partition maps to L2 bucket: "
                            f"{date}:{l2_bucket}"
                        )
                    feature_path = (
                        feature_dir
                        / f"date={date}_bucket={l2_bucket:03d}.parquet"
                    )
                    shard = _materialize_feature_partition(
                        con,
                        l2_row,
                        mapped_trades,
                        feature_path,
                        l2_bucket_count=l2_bucket_count,
                        trade_bucket_count=trade_bucket_count,
                        verify_payload_hashes=verify_payload_hashes,
                    )
                    feature_shards.append(shard)
                    partition_membership_qc.extend(
                        shard["partition_membership_qc"]
                    )
            execution_order.append(f"MATERIALIZE_{split}_PAYLOAD_COMPLETE")

        # Physical no-peek: no VALIDATE Parquet payload is opened before the
        # TRAIN model is fitted and its exact bytes are sealed on disk.
        materialize_dates(TRAIN_DATES, "TRAIN")
        feature_paths = [Path(row["path"]) for row in feature_shards]
        if not feature_paths:
            raise H4StudyError("no TRAIN feature partitions were materialized")
        execution_order.append("FIT_TRAIN_ONLY_MODEL_START")
        model = _fit_train_only_model(
            con,
            feature_paths,
            signal_quantile,
            min_train_candidates_per_sport,
        )
        execution_order.append("FIT_TRAIN_ONLY_MODEL_COMPLETE")
        model_seal_path = output_dir / MODEL_SEAL_NAME
        model_seal_bytes = _canonical_bytes(model)
        _atomic_write(model_seal_path, model_seal_bytes)
        model_seal_sha256 = _sha256_path(model_seal_path)
        if (
            model_seal_sha256 != _sha256_bytes(model_seal_bytes)
            or json.loads(model_seal_path.read_bytes()) != model
        ):
            raise H4StudyError("TRAIN model seal verification failed")
        execution_order.append("SEAL_TRAIN_MODEL_COMPLETE")

        materialize_dates(VALIDATE_DATES, "VALIDATE")
        feature_paths = [Path(row["path"]) for row in feature_shards]
        execution_order.append("EVALUATE_FROZEN_MODEL_START")
        aggregate_rows: List[Dict[str, Any]] = []
        if model["locked_sports"]:
            for path in feature_paths:
                aggregate_rows.extend(
                    _rows(con, _aggregate_shard_sql(path, model))
                )
        results = _complete_result_grid(
            _merge_aggregates(aggregate_rows), model["locked_sports"]
        )
        diagnostics = _input_diagnostics(
            con,
            feature_paths,
            feature_shards,
            model["locked_sports"],
            partition_membership_qc,
        )
        execution_order.append("EVALUATE_FROZEN_MODEL_COMPLETE")
    finally:
        con.close()

    generated_at = dt.datetime.now(dt.timezone.utc).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
    receipt: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "state": "COMPLETE",
        "generated_at_utc": generated_at,
        "run_id": (
            "h4-"
            + generated_at.replace("-", "").replace(":", "")
            + "-"
            + l2_stage["source_binding"][:12]
        ),
        "module_sha256": _module_sha256(),
        "resource_contract": {
            "duckdb_memory_limit": MEMORY_LIMIT,
            "duckdb_threads": THREADS,
            "partitioning": (
                "one L2 hash bucket plus congruent trade hash buckets per query"
            ),
            "full_corpus_collected_in_python_memory": False,
        },
        "split_contract": {
            "train_dates": list(TRAIN_DATES),
            "validate_dates": list(VALIDATE_DATES),
            "validation_used_for_sport_selection": False,
            "validation_used_for_feature_scale": False,
            "validation_used_for_threshold": False,
            "validation_payload_read_before_model_seal": False,
            "model_sealed_before_first_validation_payload_read": True,
            "model_seal_sha256": model_seal_sha256,
            "execution_order": execution_order,
            "validation_status": (
                "H4_SPRINT_HELD_OUT_ONLY_NOT_GLOBALLY_UNTOUCHED_CONFIRMATION"
            ),
        },
        "source": {
            "checkpoint_namespace": str(checkpoint_namespace),
            "source_binding": l2_stage["source_binding"],
            "payload_hashes_recomputed": verify_payload_hashes,
            "l2_replay": {
                "stage_version": l2_stage["stage_version"],
                "manifest_sha256": l2_stage["manifest_sha256"],
                "market_bucket_count": layout["l2_bucket_count"],
            },
            "trades_market": {
                "stage_version": trade_stage["stage_version"],
                "manifest_sha256": trade_stage["manifest_sha256"],
                "market_bucket_count": layout["trade_bucket_count"],
            },
        },
        "feature_contract": {
            "schema_version": FEATURE_SCHEMA_VERSION,
            "clock": "local_recv_ts_us/effective t_us",
            "lookback_us": LOOKBACK_US,
            "trade_interval": "[decision_t_us-1s,decision_t_us)",
            "same_timestamp_trade_included": False,
            "signed_flow": "YES taker +count_e4; NO taker -count_e4",
            "quote_velocity": (
                "current microprice (mid fallback) minus effective quote at or "
                "before decision_t_us-1s"
            ),
            "depth_imbalance": "current replay imbalance_depth3",
            "candidate_event": (
                "classification=DELTA_APPLIED, top_changed=true, and "
                "non-null snapshot_epoch; snapshots/reconnect anchors excluded"
            ),
            "quote_velocity_epoch_rule": (
                "past quote and decision must share snapshot_epoch"
            ),
            "one_contract_depth_e4": ONE_CONTRACT_QTY_E4,
        },
        "model": model,
        "horizons_us": list(HORIZONS_US),
        "horizons": [
            {
                "horizon_us": horizon,
                "role": (
                    "PRIMARY"
                    if horizon in (100_000, 1_000_000)
                    else "SENSITIVITY_DISPLAYED_STATE_PROXY"
                ),
                "max_effective_quote_age_us": MAX_OUTCOME_QUOTE_AGE_US,
            }
            for horizon in HORIZONS_US
        ],
        "controls": [
            "BASELINE",
            "REVERSE",
            "PLACEBO_HASH_DIRECTION",
        ],
        "diagnostics": diagnostics,
        "results": results,
        "claims": {
            "cash_pnl": False,
            "observed_fills": False,
            "fees_included": False,
            "decision_to_order_latency_included": False,
            "portfolio_overlap_netting_included": False,
            "return_label": (
                "ONE_CONTRACT_DISPLAYED_TOUCH_GROSS_RETURN_EVENT_STUDY"
            ),
            "primary_horizons_us": [100_000, 1_000_000],
            "sensitivity_displayed_state_proxy_horizons_us": [5_000_000],
            "primary_outcome_quote_ttl_us": MAX_OUTCOME_QUOTE_AGE_US,
            "phase": "UNKNOWN_PHASE_ONLY",
            "live_or_pregame_stratification_claimed": False,
            "profitability_claimed": False,
        },
    }
    report_text = _render_markdown(receipt)
    report_path = output_dir / REPORT_NAME
    _atomic_write(report_path, report_text.encode("utf-8"))
    receipt["artifacts"] = {
        "train_model_seal": {
            "path": MODEL_SEAL_NAME,
            "sha256": model_seal_sha256,
        },
        "report": {
            "path": REPORT_NAME,
            "sha256": _sha256_path(report_path),
        }
    }
    receipt_path = output_dir / RECEIPT_NAME
    _atomic_write(receipt_path, _canonical_bytes(receipt))
    if not keep_scratch:
        shutil.rmtree(scratch)
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the offline H4 past-only order-flow event study"
    )
    parser.add_argument(
        "--checkpoint-namespace",
        type=Path,
        required=True,
        help=(
            "exact Deep03 source-* checkpoint namespace containing "
            "trades_market and l2_replay"
        ),
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--verify-payload-hashes",
        action="store_true",
        help="recompute every used checkpoint payload SHA-256 (slower)",
    )
    parser.add_argument(
        "--keep-scratch",
        action="store_true",
        help="retain intermediate feature Parquet shards for audit",
    )
    parser.add_argument(
        "--signal-quantile",
        type=float,
        default=DEFAULT_SIGNAL_QUANTILE,
    )
    parser.add_argument(
        "--min-train-candidates-per-sport",
        type=int,
        default=DEFAULT_MIN_TRAIN_CANDIDATES_PER_SPORT,
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = run_event_study(
            args.checkpoint_namespace,
            args.output_dir,
            verify_payload_hashes=args.verify_payload_hashes,
            keep_scratch=args.keep_scratch,
            signal_quantile=args.signal_quantile,
            min_train_candidates_per_sport=args.min_train_candidates_per_sport,
        )
    except (H4StudyError, OSError, duckdb.Error) as exc:
        print(f"H4_EVENT_STUDY_REFUSED: {exc}", file=sys.stderr)
        return 2
    print(
        "H4_EVENT_STUDY_COMPLETE "
        f"receipt={Path(args.output_dir).resolve() / RECEIPT_NAME} "
        f"report={Path(args.output_dir).resolve() / REPORT_NAME} "
        f"model_sha256={receipt['model']['model_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
