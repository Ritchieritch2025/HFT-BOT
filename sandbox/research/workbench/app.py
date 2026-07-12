#!/usr/bin/env python3
"""Local, read-only HTTP workbench for sports-market research artifacts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import ipaddress
import json
import math
import os
from collections import defaultdict
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


WORKBENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = WORKBENCH_DIR.parents[2]
HYPOTHESES_PATH = WORKBENCH_DIR / "hypotheses.json"
INDEX_PATH = WORKBENCH_DIR / "index.html"
ECHARTS_PATH = REPO_ROOT / "docs" / "vendor" / "js" / "echarts.min.js"
SPORTS = ("Soccer", "Baseball", "Basketball", "Tennis")
OVERVIEW_SCHEMA = "research-workbench-overview-v1"
HYPOTHESES_SCHEMA = "research-workbench-hypotheses-v1"
EXPERIMENTS_SCHEMA = "research-workbench-experiments-v1"
EXPERIMENT_SCHEMA = "research-experiment-v1"
LIQUIDITY_SCHEMA = "research-workbench-liquidity-v1"
MAIN_EVENTS_SCHEMA = "research-workbench-main-events-v1"
RESULT_STATUSES = {"NO_RESULTS", "EXPLORATORY", "INCONCLUSIVE", "PASS", "FAIL"}
HYPOTHESIS_STATUSES = {
    "DRAFT_NEEDS_FREEZE",
    "FROZEN_EXPLORATORY",
    "FROZEN_TRAIN",
    "RETIRED",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _repo_paths(repo_root: Path) -> tuple[Path, Path, Path]:
    research = repo_root / "sandbox" / "research"
    return (
        repo_root / "work" / "warehouse" / "manifest.csv",
        research / "reports" / "experiments",
        research / "reports" / "workbench",
    )


def _display_path(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_hypotheses(path: Path = HYPOTHESES_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        registry = json.load(handle)
    if not isinstance(registry, dict) or registry.get("schema_version") != HYPOTHESES_SCHEMA:
        raise ValueError(f"invalid hypotheses registry: expected {HYPOTHESES_SCHEMA}")
    hypotheses = registry.get("hypotheses")
    if not isinstance(hypotheses, list):
        raise ValueError("invalid hypotheses registry: hypotheses must be a list")
    seen: set[str] = set()
    for item in hypotheses:
        if not isinstance(item, dict):
            raise ValueError("invalid hypotheses registry: each hypothesis must be an object")
        hypothesis_id = item.get("hypothesis_id")
        if not isinstance(hypothesis_id, str) or not hypothesis_id:
            raise ValueError("invalid hypotheses registry: hypothesis_id is required")
        if hypothesis_id in seen:
            raise ValueError(f"invalid hypotheses registry: duplicate {hypothesis_id}")
        if item.get("status") not in HYPOTHESIS_STATUSES:
            raise ValueError(
                f"invalid hypotheses registry: {hypothesis_id} status must be one of "
                + ", ".join(sorted(HYPOTHESIS_STATUSES))
            )
        seen.add(hypothesis_id)
    return registry


def aggregate_manifest(manifest_path: Path, repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    cells: dict[str, dict[tuple[str, str], int]] = {
        sport: defaultdict(int) for sport in SPORTS
    }
    total_rows = 0
    matched_rows = 0
    invalid_rows = 0
    errors: list[str] = []
    available = manifest_path.is_file()
    digest: str | None = None

    if available:
        digest = _sha256(manifest_path)
        sport_lookup = {sport.casefold(): sport for sport in SPORTS}
        with manifest_path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            required = {"date", "table", "category", "subcategory", "row_count"}
            missing = sorted(required.difference(reader.fieldnames or []))
            if missing:
                raise ValueError("manifest missing columns: " + ", ".join(missing))
            for row in reader:
                total_rows += 1
                if (row.get("category") or "").strip().casefold() != "sports":
                    continue
                sport = sport_lookup.get((row.get("subcategory") or "").strip().casefold())
                if sport is None:
                    continue
                matched_rows += 1
                day = (row.get("date") or "").strip()
                table = (row.get("table") or "").strip()
                try:
                    if date.fromisoformat(day).isoformat() != day or not table:
                        raise ValueError
                    count = int((row.get("row_count") or "").strip())
                    if count < 0:
                        raise ValueError
                except ValueError:
                    invalid_rows += 1
                    continue
                cells[sport][(day, table)] += count
    else:
        errors.append("source manifest not found")

    all_dates = sorted({day for sport in SPORTS for day, _ in cells[sport]})
    all_tables = sorted({table for sport in SPORTS for _, table in cells[sport]})
    sports = []
    for sport in SPORTS:
        by_date = [
            {"date": day, "table": table, "rows": rows}
            for (day, table), rows in sorted(cells[sport].items())
        ]
        rows_by_table: dict[str, int] = defaultdict(int)
        for item in by_date:
            rows_by_table[item["table"]] += item["rows"]
        sports.append(
            {
                "sport": sport,
                "total_rows": sum(item["rows"] for item in by_date),
                "rows_by_table": dict(sorted(rows_by_table.items())),
                "by_date": by_date,
            }
        )

    return {
        "path": _display_path(manifest_path, repo_root),
        "sha256": digest,
        "available": available,
        "total_manifest_rows": total_rows,
        "matched_rows": matched_rows,
        "invalid_rows": invalid_rows,
        "errors": errors,
        "dates": all_dates,
        "tables": all_tables,
        "sports": sports,
    }


LIQUIDITY_METRICS: dict[str, dict[str, Any]] = {
    "spread_cents": {
        "label": "Quoted spread",
        "unit": "cents",
        "better": "lower",
        "plain_meaning": "Distance between the best YES ask and best YES bid at the hourly state sample.",
        "formula": "(yes_ask_e4 - yes_bid_e4) / 100",
        "provenance": "orderbooks_l1 hourly heartbeat rows (is_snapshot=true)",
    },
    "touch_depth_contracts": {
        "label": "Two-sided touch depth",
        "unit": "contracts",
        "better": "higher",
        "plain_meaning": "The smaller displayed size on the two best YES sides; a conservative top-of-book depth proxy.",
        "formula": "min(yes_bid_qty_e4, yes_ask_qty_e4) / 10,000",
        "provenance": "orderbooks_l1 hourly heartbeat rows (is_snapshot=true)",
    },
    "l1_changes_per_hour": {
        "label": "L1 state changes",
        "unit": "changes / market-hour",
        "better": "context only",
        "plain_meaning": "How many non-heartbeat best-quote state changes were archived for one market during the UTC hour.",
        "formula": "count(orderbooks_l1 rows where is_snapshot=false) per market-hour",
        "provenance": "orderbooks_l1 change-only rows; this is not raw packet rate",
    },
    "trade_count": {
        "label": "Trades",
        "unit": "unique trades / market-hour",
        "better": "higher activity",
        "plain_meaning": "Distinct, internally consistent trade IDs observed for one market during the UTC hour.",
        "formula": "count(distinct trade_id) after exact-duplicate collapse and conflicting-body exclusion",
        "provenance": "trades csv.gz facts, explicit VARCHAR schema",
    },
    "trade_volume_contracts": {
        "label": "Contract volume",
        "unit": "contracts / market-hour",
        "better": "higher activity",
        "plain_meaning": "Total contracts traded in one market during the UTC hour.",
        "formula": "sum(deduplicated count_e4) / 10,000",
        "provenance": "trades csv.gz facts; conflicting trade IDs excluded",
    },
}


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_file_list(paths: list[Path]) -> str:
    return "[" + ",".join(_sql_string(str(path.resolve())) for path in paths) + "]"


def _iso_from_epoch_us(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1_000_000, tz=timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _clean_number(value: Any) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _metric_payload(
    row: dict[str, Any], metric: str, probabilities: list[float]
) -> dict[str, Any]:
    values = row[f"{metric}__quantiles"] or []
    ecdf = [
        [_clean_number(value), probability]
        for value, probability in zip(values, probabilities)
        if _clean_number(value) is not None
    ]
    return {
        "n": int(row[f"{metric}__n"] or 0),
        "p50": _clean_number(row[f"{metric}__p50"]),
        "p99": _clean_number(row[f"{metric}__p99"]),
        "max": _clean_number(row[f"{metric}__max"]),
        "ecdf": ecdf,
    }


def _empty_liquidity(
    generated_at_utc: str, manifest_path: Path, repo_root: Path, reason: str
) -> dict[str, Any]:
    return {
        "schema_version": LIQUIDITY_SCHEMA,
        "generated_at_utc": generated_at_utc,
        "available": False,
        "tier": "EXPLORATORY",
        "quality_label": "NO LOCAL LIQUIDITY SNAPSHOT",
        "reason": reason,
        "dates": [],
        "sports": [],
        "metrics": LIQUIDITY_METRICS,
        "slices": [],
        "daily_coverage": [],
        "source": {
            "manifest_path": _display_path(manifest_path, repo_root),
            "manifest_sha256": _sha256(manifest_path) if manifest_path.is_file() else None,
        },
    }


def build_liquidity_snapshot(
    repo_root: Path = REPO_ROOT,
    generated_at_utc: str | None = None,
) -> dict[str, Any]:
    """Build bounded hourly liquidity distributions from archived local facts.

    State metrics intentionally use one ingestion heartbeat per market-hour rather
    than change rows. That prevents fast-changing markets from receiving more
    statistical weight merely because they update more often.
    """

    repo_root = Path(repo_root)
    generated_at_utc = generated_at_utc or _utc_now()
    manifest_path, _, _ = _repo_paths(repo_root)
    facts = repo_root / "work" / "warehouse" / "facts"
    l1_files: list[Path] = []
    trade_files: list[Path] = []
    for sport in SPORTS:
        l1_files.extend(
            sorted(
                (
                    facts
                    / "orderbooks_l1"
                    / "category=Sports"
                    / f"subcategory={sport}"
                ).glob("date=*/*.parquet")
            )
        )
        trade_files.extend(
            sorted(
                (facts / "trades" / "category=Sports" / f"subcategory={sport}").glob(
                    "date=*/*.csv.gz"
                )
            )
        )
    if not l1_files:
        return _empty_liquidity(
            generated_at_utc,
            manifest_path,
            repo_root,
            "no archived Sports L1 parquet files found",
        )

    try:
        import duckdb  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        return _empty_liquidity(
            generated_at_utc,
            manifest_path,
            repo_root,
            "duckdb Python module is unavailable",
        )

    connection = duckdb.connect(database=":memory:")
    connection.execute("SET threads=2")
    connection.execute("SET preserve_insertion_order=false")
    l1_sources = _sql_file_list(l1_files)

    connection.execute(
        f"""
        CREATE TEMP TABLE snapshot_states AS
        SELECT
            subcategory AS sport,
            CAST(date AS VARCHAR) AS date_value,
            EXTRACT('hour' FROM make_timestamp(ts_utc))::INTEGER AS hour_utc,
            market_ticker,
            MAX(ts_utc) AS snapshot_ts_utc,
            arg_max(yes_bid_e4, ts_utc) AS yes_bid_e4,
            arg_max(yes_ask_e4, ts_utc) AS yes_ask_e4,
            arg_max(yes_bid_qty_e4, ts_utc) AS yes_bid_qty_e4,
            arg_max(yes_ask_qty_e4, ts_utc) AS yes_ask_qty_e4
        FROM read_parquet({l1_sources}, hive_partitioning=1)
        WHERE is_snapshot=true
        GROUP BY ALL
        """
    )
    connection.execute(
        f"""
        CREATE TEMP TABLE l1_changes AS
        SELECT
            subcategory AS sport,
            CAST(date AS VARCHAR) AS date_value,
            EXTRACT('hour' FROM make_timestamp(ts_utc))::INTEGER AS hour_utc,
            market_ticker,
            COUNT(*)::INTEGER AS l1_changes
        FROM read_parquet({l1_sources}, hive_partitioning=1)
        WHERE is_snapshot=false
        GROUP BY ALL
        """
    )

    connection.execute(
        """
        CREATE TEMP TABLE safe_trades (
            sport VARCHAR,
            date_value VARCHAR,
            hour_utc INTEGER,
            market_ticker VARCHAR,
            trade_count INTEGER,
            trade_volume_contracts DOUBLE
        )
        """
    )
    conflicting_trade_ids = 0
    raw_trade_rows = 0
    safe_trade_rows = 0
    if trade_files:
        trade_sources = _sql_file_list(trade_files)
        trade_columns = (
            "{'ts_utc':'BIGINT','market_ticker':'VARCHAR','series_ticker':'VARCHAR',"
            "'event_ticker':'VARCHAR','category':'VARCHAR','subcategory':'VARCHAR',"
            "'group':'VARCHAR','trade_id':'VARCHAR','yes_price_e4':'INTEGER',"
            "'no_price_e4':'INTEGER','count_e4':'BIGINT','taker_side':'VARCHAR'}"
        )
        connection.execute(
            f"""
            CREATE TEMP TABLE trade_bodies AS
            SELECT
                trade_id,
                any_value(subcategory) AS sport,
                any_value(market_ticker) AS market_ticker,
                any_value(ts_utc) AS ts_utc,
                any_value(count_e4) AS count_e4,
                COUNT(*)::INTEGER AS raw_rows,
                COUNT(DISTINCT hash(
                    ts_utc, market_ticker, yes_price_e4, no_price_e4,
                    count_e4, taker_side
                ))::INTEGER AS variants
            FROM read_csv(
                {trade_sources}, columns={trade_columns}, header=true,
                compression='gzip'
            )
            GROUP BY trade_id
            """
        )
        raw_trade_rows, safe_trade_rows, conflicting_trade_ids = connection.execute(
            """
            SELECT
                COALESCE(SUM(raw_rows), 0),
                COUNT(*) FILTER (WHERE variants=1),
                COUNT(*) FILTER (WHERE variants>1)
            FROM trade_bodies
            """
        ).fetchone()
        connection.execute(
            """
            INSERT INTO safe_trades
            SELECT
                sport,
                CAST(date(make_timestamp(ts_utc)) AS VARCHAR) AS date_value,
                EXTRACT('hour' FROM make_timestamp(ts_utc))::INTEGER AS hour_utc,
                market_ticker,
                COUNT(*)::INTEGER AS trade_count,
                SUM(count_e4) / 10000.0 AS trade_volume_contracts
            FROM trade_bodies
            WHERE variants=1
            GROUP BY ALL
            """
        )

    connection.execute(
        """
        CREATE TEMP TABLE market_hours AS
        SELECT
            state.sport,
            state.date_value,
            state.hour_utc,
            state.market_ticker,
            state.snapshot_ts_utc,
            CASE WHEN
                state.yes_bid_e4 > 0 AND state.yes_ask_e4 < 10000
                AND state.yes_ask_e4 >= state.yes_bid_e4
                AND state.yes_bid_qty_e4 > 0 AND state.yes_ask_qty_e4 > 0
            THEN (state.yes_ask_e4 - state.yes_bid_e4) / 100.0 END AS spread_cents,
            CASE WHEN
                state.yes_bid_e4 > 0 AND state.yes_ask_e4 < 10000
                AND state.yes_ask_e4 >= state.yes_bid_e4
                AND state.yes_bid_qty_e4 > 0 AND state.yes_ask_qty_e4 > 0
            THEN least(state.yes_bid_qty_e4, state.yes_ask_qty_e4) / 10000.0 END
                AS touch_depth_contracts,
            COALESCE(changes.l1_changes, 0) AS l1_changes_per_hour,
            COALESCE(trades.trade_count, 0) AS trade_count,
            COALESCE(trades.trade_volume_contracts, 0) AS trade_volume_contracts
        FROM snapshot_states AS state
        LEFT JOIN l1_changes AS changes
            USING (sport, date_value, hour_utc, market_ticker)
        LEFT JOIN safe_trades AS trades
            USING (sport, date_value, hour_utc, market_ticker)
        """
    )

    probabilities = [round(index / 100, 2) for index in range(101)]
    probability_sql = "[" + ",".join(str(value) for value in probabilities) + "]"
    metric_names = list(LIQUIDITY_METRICS)
    metric_sql = []
    for metric in metric_names:
        metric_sql.extend(
            (
                f"COUNT({metric})::INTEGER AS {metric}__n",
                f"quantile_cont({metric}, 0.5) AS {metric}__p50",
                f"quantile_cont({metric}, 0.99) AS {metric}__p99",
                f"MAX({metric}) AS {metric}__max",
                f"quantile_cont({metric}, {probability_sql}) AS {metric}__quantiles",
            )
        )
    metrics_projection = ",\n".join(metric_sql)
    grouped_sql = f"""
        SELECT sport, date_value AS date_scope, hour_utc,
            COUNT(*)::INTEGER AS active_market_count,
            100.0 * COUNT(spread_cents) / COUNT(*) AS two_sided_market_share_pct,
            {metrics_projection}
        FROM market_hours
        GROUP BY ALL
        UNION ALL
        SELECT sport, 'ALL' AS date_scope, hour_utc,
            COUNT(*)::INTEGER AS active_market_count,
            100.0 * COUNT(spread_cents) / COUNT(*) AS two_sided_market_share_pct,
            {metrics_projection}
        FROM market_hours
        GROUP BY sport, hour_utc
        ORDER BY sport, date_scope, hour_utc
    """
    result = connection.execute(grouped_sql)
    names = [column[0] for column in result.description]
    grouped_rows = [dict(zip(names, row)) for row in result.fetchall()]

    daily_hour_rows = connection.execute(
        """
        SELECT sport, date_value, hour_utc,
            COUNT(*)::INTEGER AS active_market_count,
            100.0 * COUNT(spread_cents) / COUNT(*) AS two_sided_market_share_pct
        FROM market_hours
        GROUP BY ALL
        """
    ).fetchall()
    availability: dict[tuple[str, str, int], dict[str, Any]] = {}
    per_all: dict[tuple[str, int], list[tuple[int, float]]] = defaultdict(list)
    for sport, date_value, hour_utc, active_count, two_sided_share in daily_hour_rows:
        availability[(sport, date_value, hour_utc)] = {
            "n_days": 1,
            "active_markets_p50": active_count,
            "active_markets_p99": active_count,
            "active_markets_max": active_count,
            "two_sided_share_p50": _clean_number(two_sided_share),
            "two_sided_share_p99": _clean_number(two_sided_share),
            "two_sided_share_max": _clean_number(two_sided_share),
        }
        per_all[(sport, hour_utc)].append((active_count, float(two_sided_share)))
    for (sport, hour_utc), observations in per_all.items():
        active = sorted(value[0] for value in observations)
        shares = sorted(value[1] for value in observations)

        def percentile(values: list[float | int], probability: float) -> float:
            if len(values) == 1:
                return float(values[0])
            position = probability * (len(values) - 1)
            lower = math.floor(position)
            upper = math.ceil(position)
            if lower == upper:
                return float(values[lower])
            weight = position - lower
            return float(values[lower]) * (1 - weight) + float(values[upper]) * weight

        availability[(sport, "ALL", hour_utc)] = {
            "n_days": len(observations),
            "active_markets_p50": percentile(active, 0.5),
            "active_markets_p99": percentile(active, 0.99),
            "active_markets_max": max(active),
            "two_sided_share_p50": percentile(shares, 0.5),
            "two_sided_share_p99": percentile(shares, 0.99),
            "two_sided_share_max": max(shares),
        }

    slices = []
    for row in grouped_rows:
        key = (row["sport"], row["date_scope"], row["hour_utc"])
        slices.append(
            {
                "sport": row["sport"],
                "date": row["date_scope"],
                "hour_utc": row["hour_utc"],
                "availability": availability.get(key, {}),
                "metrics": {
                    metric: _metric_payload(row, metric, probabilities)
                    for metric in metric_names
                },
            }
        )

    coverage_rows = connection.execute(
        """
        SELECT
            hours.sport,
            hours.date_value,
            COUNT(DISTINCT hours.market_ticker)::INTEGER AS markets,
            COUNT(*)::INTEGER AS hourly_state_samples,
            MIN(hours.snapshot_ts_utc) AS first_snapshot_us,
            MAX(hours.snapshot_ts_utc) AS last_snapshot_us,
            COALESCE(SUM(trades.trade_count), 0)::INTEGER AS safe_trade_rows
        FROM market_hours AS hours
        LEFT JOIN safe_trades AS trades
            USING (sport, date_value, hour_utc, market_ticker)
        GROUP BY hours.sport, hours.date_value
        ORDER BY hours.sport, hours.date_value
        """
    ).fetchall()
    daily_coverage = [
        {
            "sport": sport,
            "date": date_value,
            "markets": markets,
            "hourly_state_samples": hourly_samples,
            "first_snapshot_utc": _iso_from_epoch_us(first_us),
            "last_snapshot_utc": _iso_from_epoch_us(last_us),
            "safe_trade_rows": safe_rows,
        }
        for sport, date_value, markets, hourly_samples, first_us, last_us, safe_rows
        in coverage_rows
    ]
    dates = sorted({row["date"] for row in daily_coverage})
    sports = [sport for sport in SPORTS if any(row["sport"] == sport for row in daily_coverage)]
    connection.close()

    return {
        "schema_version": LIQUIDITY_SCHEMA,
        "generated_at_utc": generated_at_utc,
        "available": True,
        "tier": "EXPLORATORY",
        "quality_label": "UNSEALED / GAP-DEGRADED",
        "title": "Liquidity Atlas — hourly market states",
        "dates": dates,
        "sports": sports,
        "time_basis": {
            "label": "Absolute UTC hour",
            "formula": "floor(exchange ts_utc to UTC hour)",
            "blocked_alternative": "time-to-event is unavailable because historical fact tickers do not join to a verified occurrence_datetime",
        },
        "analysis_unit": "market × UTC hour × archive date",
        "aggregation": (
            "One latest is_snapshot=true heartbeat per market-hour supplies the state. "
            "Distributions are across market-hours, so fast-updating markets do not get extra weight."
        ),
        "metrics": LIQUIDITY_METRICS,
        "slices": slices,
        "daily_coverage": daily_coverage,
        "trade_quality": {
            "raw_rows": int(raw_trade_rows),
            "safe_unique_trade_ids": int(safe_trade_rows),
            "conflicting_trade_ids_excluded": int(conflicting_trade_ids),
        },
        "limitations": [
            "Local archive covers dated 2026-07-06..08 facts, not the latest production days.",
            "No local day seals exist and capture gaps are known; missing observations are not proof of an inactive market.",
            "Depth is top-of-book only. Historical L2 is effectively absent, and live L2/RFQ are not included here.",
            "Absolute UTC hour is descriptive calendar time, not verified time-to-kickoff or in-play state.",
            "L1 change count is an archived state-change count, not raw WebSocket packet or exchange message rate.",
        ],
        "source": {
            "manifest_path": _display_path(manifest_path, repo_root),
            "manifest_sha256": _sha256(manifest_path) if manifest_path.is_file() else None,
            "l1_file_count": len(l1_files),
            "trade_file_count": len(trade_files),
            "l1_path_pattern": "work/warehouse/facts/orderbooks_l1/category=Sports/subcategory=<sport>/date=*/*.parquet",
            "trade_path_pattern": "work/warehouse/facts/trades/category=Sports/subcategory=<sport>/date=*/*.csv.gz",
            "code_location": "sandbox/research/workbench/app.py::build_liquidity_snapshot",
        },
    }


def _empty_main_events(
    generated_at_utc: str, repo_root: Path, reason: str
) -> dict[str, Any]:
    manifest_path, _, _ = _repo_paths(repo_root)
    return {
        "schema_version": MAIN_EVENTS_SCHEMA,
        "generated_at_utc": generated_at_utc,
        "available": False,
        "tier": "EXPLORATORY",
        "quality_label": "NO LARGE-ACTIVITY SNAPSHOT",
        "reason": reason,
        "sports": [],
        "episodes": [],
        "source": {
            "manifest_path": _display_path(manifest_path, repo_root),
            "manifest_sha256": _sha256(manifest_path) if manifest_path.is_file() else None,
        },
    }


def _distribution_payload(
    row: dict[str, Any] | None, prefix: str, probabilities: list[float]
) -> dict[str, Any]:
    if not row:
        return {"n": 0, "p50": None, "p99": None, "max": None, "ecdf": []}
    return _metric_payload(row, prefix, probabilities)


def build_main_events_snapshot(
    repo_root: Path = REPO_ROOT,
    generated_at_utc: str | None = None,
) -> dict[str, Any]:
    """Build a bounded large-print and L1 touch-addition explorer.

    The artifact never identifies an actor. Trade IDs are transaction IDs and
    L1 quantities are aggregate displayed states, so every marker remains a
    large-activity candidate rather than a person/order attribution.
    """

    repo_root = Path(repo_root)
    generated_at_utc = generated_at_utc or _utc_now()
    manifest_path, _, _ = _repo_paths(repo_root)
    facts = repo_root / "work" / "warehouse" / "facts"
    l1_files: list[Path] = []
    trade_files: list[Path] = []
    for sport in SPORTS:
        l1_files.extend(
            sorted(
                (
                    facts
                    / "orderbooks_l1"
                    / "category=Sports"
                    / f"subcategory={sport}"
                ).glob("date=*/*.parquet")
            )
        )
        trade_files.extend(
            sorted(
                (facts / "trades" / "category=Sports" / f"subcategory={sport}").glob(
                    "date=*/*.csv.gz"
                )
            )
        )
    if not l1_files or not trade_files:
        return _empty_main_events(
            generated_at_utc,
            repo_root,
            "archived Sports L1 and trade facts are both required",
        )
    try:
        import duckdb  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        return _empty_main_events(
            generated_at_utc, repo_root, "duckdb Python module is unavailable"
        )

    connection = duckdb.connect(database=":memory:")
    connection.execute("SET threads=2")
    connection.execute("SET preserve_insertion_order=false")
    l1_sources = _sql_file_list(l1_files)
    trade_sources = _sql_file_list(trade_files)
    trade_columns = (
        "{'ts_utc':'BIGINT','market_ticker':'VARCHAR','series_ticker':'VARCHAR',"
        "'event_ticker':'VARCHAR','category':'VARCHAR','subcategory':'VARCHAR',"
        "'group':'VARCHAR','trade_id':'VARCHAR','yes_price_e4':'INTEGER',"
        "'no_price_e4':'INTEGER','count_e4':'BIGINT','taker_side':'VARCHAR'}"
    )
    connection.execute(
        f"""
        CREATE TEMP TABLE trade_bodies AS
        SELECT
            trade_id,
            any_value(ts_utc) AS ts_utc,
            any_value(market_ticker) AS market_ticker,
            any_value(event_ticker) AS event_ticker,
            any_value(subcategory) AS sport,
            any_value("group") AS market_group,
            any_value(count_e4) AS count_e4,
            any_value(yes_price_e4) AS yes_price_e4,
            any_value(no_price_e4) AS no_price_e4,
            any_value(taker_side) AS taker_side,
            COUNT(*)::INTEGER AS raw_rows,
            COUNT(DISTINCT hash(
                ts_utc, market_ticker, yes_price_e4, no_price_e4,
                count_e4, taker_side
            ))::INTEGER AS variants
        FROM read_csv(
            {trade_sources}, columns={trade_columns}, header=true,
            compression='gzip'
        )
        GROUP BY trade_id
        """
    )
    connection.execute(
        """
        CREATE TEMP TABLE safe_trade_events AS
        SELECT
            *,
            regexp_extract(event_ticker, '-(26[A-Z0-9]+)$', 1) AS episode_key,
            count_e4 / 10000.0 AS contracts,
            yes_price_e4 / 100.0 AS yes_price_cents,
            CASE
                WHEN taker_side='yes' THEN count_e4::HUGEINT * yes_price_e4
                WHEN taker_side='no' THEN count_e4::HUGEINT * no_price_e4
            END / 100000000.0 AS taker_cash_dollars
        FROM trade_bodies
        WHERE variants=1
          AND regexp_matches(
              regexp_extract(event_ticker, '-(26[A-Z0-9]+)$', 1),
              '^26JUL[0-9]{2}[A-Z0-9]+$'
          )
        """
    )

    event_snapshot_files = sorted(
        (repo_root / "work" / "warehouse" / "dim" / "snapshots").glob(
            "date=*/events.csv"
        )
    )
    connection.execute(
        "CREATE TEMP TABLE event_meta (event_ticker VARCHAR, title VARCHAR, sub_title VARCHAR)"
    )
    if event_snapshot_files:
        event_sources = _sql_file_list(event_snapshot_files)
        connection.execute(
            f"""
            INSERT INTO event_meta
            SELECT event_ticker, any_value(title), any_value(sub_title)
            FROM read_csv_auto(
                {event_sources}, union_by_name=true, hive_partitioning=1
            )
            GROUP BY event_ticker
            """
        )

    connection.execute(
        """
        CREATE TEMP TABLE episode_stats AS
        WITH stats AS (
            SELECT
                sport,
                episode_key,
                COUNT(*)::INTEGER AS trade_count,
                SUM(contracts) AS volume_contracts,
                COUNT(DISTINCT event_ticker)::INTEGER AS event_count,
                COUNT(DISTINCT market_ticker)::INTEGER AS market_count,
                MIN(ts_utc) AS first_ts_utc,
                MAX(ts_utc) AS last_ts_utc
            FROM safe_trade_events
            GROUP BY sport, episode_key
        ), labels AS (
            SELECT
                trades.sport,
                trades.episode_key,
                arg_min(
                    COALESCE(NULLIF(meta.sub_title, ''), NULLIF(meta.title, ''), trades.event_ticker),
                    CASE
                        WHEN trades.event_ticker LIKE '%GAME-%'
                          OR trades.event_ticker LIKE '%MATCH-%' THEN 0
                        ELSE 1
                    END
                ) AS display_title
            FROM safe_trade_events AS trades
            LEFT JOIN event_meta AS meta USING (event_ticker)
            GROUP BY trades.sport, trades.episode_key
        )
        SELECT stats.*, labels.display_title
        FROM stats JOIN labels USING (sport, episode_key)
        """
    )
    connection.execute(
        """
        CREATE TEMP TABLE selected_episodes AS
        WITH ranked AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY sport
                ORDER BY volume_contracts DESC, trade_count DESC, episode_key
            ) AS rank_in_sport
            FROM episode_stats
        )
        SELECT * FROM ranked WHERE rank_in_sport <= 4
        """
    )
    connection.execute(
        """
        CREATE TEMP TABLE selected_markets AS
        WITH stats AS (
            SELECT
                trades.sport,
                trades.episode_key,
                trades.market_ticker,
                any_value(trades.event_ticker) AS event_ticker,
                any_value(trades.market_group) AS market_group,
                COUNT(*)::INTEGER AS trade_count,
                SUM(trades.contracts) AS volume_contracts
            FROM safe_trade_events AS trades
            JOIN selected_episodes USING (sport, episode_key)
            GROUP BY trades.sport, trades.episode_key, trades.market_ticker
        ), ranked AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY sport, episode_key
                ORDER BY volume_contracts DESC, trade_count DESC, market_ticker
            ) AS rank_in_episode
            FROM stats
        )
        SELECT * FROM ranked WHERE rank_in_episode <= 6
        """
    )

    connection.execute(
        f"""
        CREATE TEMP TABLE selected_l1 AS
        SELECT
            l1.ts_utc,
            l1.market_ticker,
            l1.event_ticker,
            l1.subcategory AS sport,
            l1."group" AS market_group,
            l1.yes_bid_e4,
            l1.yes_bid_qty_e4,
            l1.yes_ask_e4,
            l1.yes_ask_qty_e4,
            l1.price_e4,
            l1.is_snapshot
        FROM read_parquet({l1_sources}, hive_partitioning=1) AS l1
        JOIN selected_markets AS selected
          ON l1.market_ticker=selected.market_ticker
        """
    )
    connection.execute(
        """
        CREATE TEMP TABLE touch_candidates AS
        WITH counted AS (
            SELECT
                *,
                COUNT(*) OVER (PARTITION BY market_ticker, ts_utc) AS tied_states
            FROM selected_l1
        ), ordered AS (
            SELECT
                *,
                lag(tied_states) OVER (
                    PARTITION BY market_ticker ORDER BY ts_utc
                ) AS previous_tied_states,
                lag(yes_bid_e4) OVER (
                    PARTITION BY market_ticker ORDER BY ts_utc
                ) AS previous_bid_e4,
                lag(yes_bid_qty_e4) OVER (
                    PARTITION BY market_ticker ORDER BY ts_utc
                ) AS previous_bid_qty_e4,
                lag(yes_ask_e4) OVER (
                    PARTITION BY market_ticker ORDER BY ts_utc
                ) AS previous_ask_e4,
                lag(yes_ask_qty_e4) OVER (
                    PARTITION BY market_ticker ORDER BY ts_utc
                ) AS previous_ask_qty_e4
            FROM counted
        ), additions AS (
            SELECT
                market_ticker, event_ticker, sport, ts_utc,
                'bid_add' AS candidate_type,
                'bid' AS side,
                yes_bid_e4 / 100.0 AS price_cents,
                (yes_bid_qty_e4 - previous_bid_qty_e4) / 10000.0 AS contracts
            FROM ordered
            WHERE NOT is_snapshot AND tied_states=1 AND previous_tied_states=1
              AND yes_bid_e4=previous_bid_e4
              AND yes_bid_qty_e4>previous_bid_qty_e4
            UNION ALL
            SELECT
                market_ticker, event_ticker, sport, ts_utc,
                'ask_add' AS candidate_type,
                'ask' AS side,
                yes_ask_e4 / 100.0 AS price_cents,
                (yes_ask_qty_e4 - previous_ask_qty_e4) / 10000.0 AS contracts
            FROM ordered
            WHERE NOT is_snapshot AND tied_states=1 AND previous_tied_states=1
              AND yes_ask_e4=previous_ask_e4
              AND yes_ask_qty_e4>previous_ask_qty_e4
        )
        SELECT * FROM additions WHERE contracts>0
        """
    )

    probabilities = [round(index / 100, 2) for index in range(101)]
    probability_sql = "[" + ",".join(str(value) for value in probabilities) + "]"
    trade_distribution_result = connection.execute(
        f"""
        SELECT
            trades.market_ticker,
            COUNT(trades.contracts)::INTEGER AS trade_contracts__n,
            quantile_cont(trades.contracts, 0.5) AS trade_contracts__p50,
            quantile_cont(trades.contracts, 0.99) AS trade_contracts__p99,
            MAX(trades.contracts) AS trade_contracts__max,
            quantile_cont(trades.contracts, {probability_sql})
                AS trade_contracts__quantiles
        FROM safe_trade_events AS trades
        JOIN selected_markets AS selected USING (market_ticker)
        GROUP BY trades.market_ticker
        """
    )
    trade_distribution_names = [column[0] for column in trade_distribution_result.description]
    trade_distributions = {
        row[0]: _distribution_payload(
            dict(zip(trade_distribution_names, row)),
            "trade_contracts",
            probabilities,
        )
        for row in trade_distribution_result.fetchall()
    }
    touch_distribution_result = connection.execute(
        f"""
        SELECT
            market_ticker,
            candidate_type,
            COUNT(contracts)::INTEGER AS touch_contracts__n,
            quantile_cont(contracts, 0.5) AS touch_contracts__p50,
            quantile_cont(contracts, 0.99) AS touch_contracts__p99,
            MAX(contracts) AS touch_contracts__max,
            quantile_cont(contracts, {probability_sql}) AS touch_contracts__quantiles
        FROM touch_candidates
        GROUP BY market_ticker, candidate_type
        """
    )
    touch_distribution_names = [column[0] for column in touch_distribution_result.description]
    touch_distributions: dict[tuple[str, str], dict[str, Any]] = {}
    for row in touch_distribution_result.fetchall():
        mapping = dict(zip(touch_distribution_names, row))
        touch_distributions[(row[0], row[1])] = _distribution_payload(
            mapping, "touch_contracts", probabilities
        )

    price_rows = connection.execute(
        """
        WITH priced AS (
            SELECT
                market_ticker,
                floor(ts_utc / 60000000.0)::BIGINT * 60000000 AS minute_us,
                ts_utc,
                CASE
                    WHEN yes_bid_e4>0 AND yes_ask_e4<10000
                      AND yes_ask_e4>=yes_bid_e4
                    THEN (yes_bid_e4 + yes_ask_e4) / 200.0
                    WHEN price_e4>0 AND price_e4<10000 THEN price_e4 / 100.0
                END AS mid_cents,
                CASE
                    WHEN yes_bid_e4>0 AND yes_ask_e4<10000
                      AND yes_ask_e4>=yes_bid_e4
                    THEN (yes_ask_e4 - yes_bid_e4) / 100.0
                END AS spread_cents
            FROM selected_l1
        )
        SELECT
            market_ticker,
            minute_us / 1000 AS timestamp_ms,
            arg_max(mid_cents, ts_utc) AS mid_cents,
            arg_max(spread_cents, ts_utc) AS spread_cents
        FROM priced
        WHERE mid_cents IS NOT NULL
        GROUP BY market_ticker, minute_us
        ORDER BY market_ticker, minute_us
        """
    ).fetchall()
    price_points: dict[str, list[list[Any]]] = defaultdict(list)
    for market_ticker, timestamp_ms, mid_cents, spread_cents in price_rows:
        price_points[market_ticker].append(
            [
                int(timestamp_ms),
                _clean_number(mid_cents),
                _clean_number(spread_cents),
            ]
        )

    trade_marker_rows = connection.execute(
        """
        WITH scored AS (
            SELECT
                trades.*,
                cume_dist() OVER (
                    PARTITION BY trades.market_ticker ORDER BY trades.contracts
                ) AS percentile,
                COUNT(*) OVER (PARTITION BY trades.market_ticker) AS cohort_n
            FROM safe_trade_events AS trades
            JOIN selected_markets AS selected USING (market_ticker)
        ), retained AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY market_ticker ORDER BY contracts DESC, ts_utc
            ) AS size_rank
            FROM scored
            WHERE percentile>=0.99
        )
        SELECT
            market_ticker, ts_utc, contracts, yes_price_cents, taker_side,
            taker_cash_dollars, percentile, cohort_n, trade_id
        FROM retained
        WHERE size_rank<=1500
        ORDER BY market_ticker, ts_utc
        """
    ).fetchall()
    candidate_markers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (
        market_ticker,
        ts_utc,
        contracts,
        price_cents,
        taker_side,
        taker_cash_dollars,
        percentile,
        cohort_n,
        trade_id,
    ) in trade_marker_rows:
        candidate_markers[market_ticker].append(
            {
                "type": "trade",
                "timestamp_ms": int(ts_utc // 1000),
                "timestamp_utc": _iso_from_epoch_us(ts_utc),
                "contracts": _clean_number(contracts),
                "price_cents": _clean_number(price_cents),
                "side": taker_side if taker_side in {"yes", "no"} else "unknown",
                "taker_cash_dollars": _clean_number(taker_cash_dollars),
                "percentile": _clean_number(percentile),
                "cohort_n": int(cohort_n),
                "reference": trade_id,
                "quality_flags": [
                    "DESCRIPTIVE_FULL_SAMPLE",
                    "PRE_TL1_EXCHANGE_TIME",
                    "NO_ACTOR_IDENTITY",
                ],
            }
        )

    trade_candidate_counts = dict(
        connection.execute(
            """
            WITH scored AS (
                SELECT
                    trades.market_ticker,
                    cume_dist() OVER (
                        PARTITION BY trades.market_ticker ORDER BY trades.contracts
                    ) AS percentile
                FROM safe_trade_events AS trades
                JOIN selected_markets AS selected USING (market_ticker)
            )
            SELECT market_ticker, COUNT(*)::INTEGER
            FROM scored WHERE percentile>=0.99
            GROUP BY market_ticker
            """
        ).fetchall()
    )
    touch_candidate_counts = {
        (market_ticker, candidate_type): count
        for market_ticker, candidate_type, count in connection.execute(
            """
            WITH scored AS (
                SELECT
                    market_ticker,
                    candidate_type,
                    cume_dist() OVER (
                        PARTITION BY market_ticker, candidate_type ORDER BY contracts
                    ) AS percentile
                FROM touch_candidates
            )
            SELECT market_ticker, candidate_type, COUNT(*)::INTEGER
            FROM scored WHERE percentile>=0.99
            GROUP BY market_ticker, candidate_type
            """
        ).fetchall()
    }

    touch_marker_rows = connection.execute(
        """
        WITH scored AS (
            SELECT
                touch.*,
                cume_dist() OVER (
                    PARTITION BY market_ticker, candidate_type ORDER BY contracts
                ) AS percentile,
                COUNT(*) OVER (
                    PARTITION BY market_ticker, candidate_type
                ) AS cohort_n
            FROM touch_candidates AS touch
        ), retained AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY market_ticker, candidate_type
                ORDER BY contracts DESC, ts_utc
            ) AS size_rank
            FROM scored
            WHERE percentile>=0.99
        )
        SELECT
            market_ticker, ts_utc, candidate_type, side, contracts,
            price_cents, percentile, cohort_n
        FROM retained
        WHERE size_rank<=750
        ORDER BY market_ticker, ts_utc
        """
    ).fetchall()
    for (
        market_ticker,
        ts_utc,
        candidate_type,
        side,
        contracts,
        price_cents,
        percentile,
        cohort_n,
    ) in touch_marker_rows:
        candidate_markers[market_ticker].append(
            {
                "type": candidate_type,
                "timestamp_ms": int(ts_utc // 1000),
                "timestamp_utc": _iso_from_epoch_us(ts_utc),
                "contracts": _clean_number(contracts),
                "price_cents": _clean_number(price_cents),
                "side": side,
                "taker_cash_dollars": None,
                "percentile": _clean_number(percentile),
                "cohort_n": int(cohort_n),
                "reference": None,
                "quality_flags": [
                    "DESCRIPTIVE_FULL_SAMPLE",
                    "PRE_TL1_EXCHANGE_TIME",
                    "AGGREGATE_TOUCH_PROXY",
                    "NO_ACTOR_IDENTITY",
                ],
            }
        )

    market_rows = connection.execute(
        """
        SELECT
            sport, episode_key, market_ticker, event_ticker, market_group,
            trade_count, volume_contracts, rank_in_episode
        FROM selected_markets
        ORDER BY sport, episode_key, rank_in_episode
        """
    ).fetchall()
    markets_by_episode: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for (
        sport,
        episode_key,
        market_ticker,
        event_ticker,
        market_group,
        trade_count,
        volume_contracts,
        rank_in_episode,
    ) in market_rows:
        retained_by_type: dict[str, int] = defaultdict(int)
        for marker in candidate_markers.get(market_ticker, []):
            retained_by_type[marker["type"]] += 1
        candidate_count_by_type = {
            "trade": int(trade_candidate_counts.get(market_ticker, 0)),
            "bid_add": int(touch_candidate_counts.get((market_ticker, "bid_add"), 0)),
            "ask_add": int(touch_candidate_counts.get((market_ticker, "ask_add"), 0)),
        }
        markets_by_episode[(sport, episode_key)].append(
            {
                "market_ticker": market_ticker,
                "event_ticker": event_ticker,
                "market_group": market_group,
                "trade_count": int(trade_count),
                "volume_contracts": _clean_number(volume_contracts),
                "rank_in_episode": int(rank_in_episode),
                "price_points": price_points.get(market_ticker, []),
                "candidates": sorted(
                    candidate_markers.get(market_ticker, []),
                    key=lambda item: item["timestamp_ms"],
                ),
                "retention": {
                    marker_type: {
                        "candidate_count": candidate_count_by_type[marker_type],
                        "retained_count": retained_by_type.get(marker_type, 0),
                        "truncated_count": max(
                            0,
                            candidate_count_by_type[marker_type]
                            - retained_by_type.get(marker_type, 0),
                        ),
                    }
                    for marker_type in ("trade", "bid_add", "ask_add")
                },
                "distributions": {
                    "trade": trade_distributions.get(
                        market_ticker,
                        {"n": 0, "p50": None, "p99": None, "max": None, "ecdf": []},
                    ),
                    "bid_add": touch_distributions.get(
                        (market_ticker, "bid_add"),
                        {"n": 0, "p50": None, "p99": None, "max": None, "ecdf": []},
                    ),
                    "ask_add": touch_distributions.get(
                        (market_ticker, "ask_add"),
                        {"n": 0, "p50": None, "p99": None, "max": None, "ecdf": []},
                    ),
                },
            }
        )

    episode_rows = connection.execute(
        """
        SELECT
            sport, episode_key, display_title, trade_count, volume_contracts,
            event_count, market_count, first_ts_utc, last_ts_utc,
            rank_in_sport
        FROM selected_episodes
        ORDER BY sport, rank_in_sport
        """
    ).fetchall()
    episodes = []
    for (
        sport,
        episode_key,
        display_title,
        trade_count,
        volume_contracts,
        event_count,
        market_count,
        first_ts_utc,
        last_ts_utc,
        rank_in_sport,
    ) in episode_rows:
        episodes.append(
            {
                "sport": sport,
                "episode_key": episode_key,
                "title": display_title or episode_key,
                "trade_count": int(trade_count),
                "volume_contracts": _clean_number(volume_contracts),
                "event_count": int(event_count),
                "market_count": int(market_count),
                "first_observation_utc": _iso_from_epoch_us(first_ts_utc),
                "last_observation_utc": _iso_from_epoch_us(last_ts_utc),
                "rank_in_sport": int(rank_in_sport),
                "markets": markets_by_episode.get((sport, episode_key), []),
                "score_events": [],
            }
        )

    raw_trade_rows, safe_trade_ids, conflicting_trade_ids = connection.execute(
        """
        SELECT
            COALESCE(SUM(raw_rows), 0),
            COUNT(*) FILTER (WHERE variants=1),
            COUNT(*) FILTER (WHERE variants>1)
        FROM trade_bodies
        """
    ).fetchone()
    connection.close()

    return {
        "schema_version": MAIN_EVENTS_SCHEMA,
        "generated_at_utc": generated_at_utc,
        "available": bool(episodes),
        "tier": "EXPLORATORY",
        "quality_label": "DESCRIPTIVE FULL-SAMPLE · NO ACTOR IDENTITY",
        "title": "Main Events · Large-Print & L1 Displacement Tape",
        "sports": [sport for sport in SPORTS if any(e["sport"] == sport for e in episodes)],
        "episodes": episodes,
        "thresholds": [
            {"quantile": 0.99, "label": "p99", "minimum_cohort_n": 500},
            {"quantile": 0.995, "label": "p99.5", "minimum_cohort_n": 1000},
            {"quantile": 0.999, "label": "p99.9", "minimum_cohort_n": 5000},
        ],
        "definitions": {
            "trade": {
                "label": "Large trade print",
                "formula": "contracts=count_e4/10,000; percentile within market over the archived window",
                "meaning": "A real trade event that is large relative to other prints in the same market; not the originating order or trader.",
            },
            "bid_add": {
                "label": "Same-price bid touch increase",
                "formula": "(current_bid_qty_e4-prior_bid_qty_e4)/10,000 when best bid price is unchanged",
                "meaning": "An aggregate displayed-size increase at the best bid; it may contain one or many orders.",
            },
            "ask_add": {
                "label": "Same-price ask touch increase",
                "formula": "(current_ask_qty_e4-prior_ask_qty_e4)/10,000 when best ask price is unchanged",
                "meaning": "An aggregate displayed-size increase at the best ask; it may contain one or many orders.",
            },
        },
        "score_alignment": {
            "captured_now": False,
            "possible_prospectively": True,
            "status": "OFFICIAL ENDPOINTS AVAILABLE · RUNTIME PAYLOAD CONTRACT NOT YET CAPTURED",
            "mapping": "milestones.related_event_tickers / primary_event_tickers",
            "live_state_endpoint": "/live_data/batch",
            "play_by_play_endpoint": "/live_data/milestone/{milestone_id}/game_stats",
            "supported_sports": [
                "Pro Football",
                "College Football",
                "Pro Basketball",
                "College Basketball",
                "WNBA",
                "Soccer",
                "Pro Hockey",
                "Pro Baseball",
            ],
            "historical_backfill": "NOT_VERIFIED",
            "required_capture": [
                "append-only milestone mapping",
                "raw live_data/game_stats payload",
                "recv_wall_ns and recv_mono_ns",
                "per-sport score/period/clock schema validation",
                "duplicate, correction, gap and source-latency accounting",
            ],
        },
        "trade_quality": {
            "raw_rows": int(raw_trade_rows),
            "safe_unique_trade_ids": int(safe_trade_ids),
            "conflicting_trade_ids_excluded": int(conflicting_trade_ids),
        },
        "limitations": [
            "Trade IDs identify transactions, not accounts; no participant or actor identity is present.",
            "Historical L1 is aggregate top-of-book only; touch increases are not individual orders and can include spoofed liquidity.",
            "Percentiles use the full descriptive archive and therefore have look-ahead; strategy thresholds must later be frozen on TRAIN and applied forward.",
            "The pre-TL1 archive supplies exchange/coarse ts_utc only, not decision-time receive timestamps.",
            "No score/game-state payload was captured for these dates, so the current tape cannot attribute a move to a goal, run, point, set or period transition.",
        ],
        "source": {
            "manifest_path": _display_path(manifest_path, repo_root),
            "manifest_sha256": _sha256(manifest_path) if manifest_path.is_file() else None,
            "l1_file_count": len(l1_files),
            "trade_file_count": len(trade_files),
            "code_location": "sandbox/research/workbench/app.py::build_main_events_snapshot",
        },
    }


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def discover_experiments(
    experiments_dir: Path,
    hypothesis_statuses: dict[str, str],
    generated_at_utc: str,
) -> dict[str, Any]:
    experiments: list[dict[str, Any]] = []
    validation_errors: list[dict[str, Any]] = []

    for path in sorted(experiments_dir.glob("*.json")) if experiments_dir.is_dir() else []:
        errors: list[str] = []
        payload: dict[str, Any] = {}
        try:
            with path.open(encoding="utf-8") as handle:
                parsed = json.load(handle)
            if not isinstance(parsed, dict):
                errors.append("artifact root must be a JSON object")
            else:
                payload = parsed
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(f"unreadable JSON: {exc}")

        if payload.get("schema_version") != EXPERIMENT_SCHEMA:
            errors.append(f"schema_version must be {EXPERIMENT_SCHEMA}")
        for field in ("experiment_id", "title"):
            if not _nonempty_string(payload.get(field)):
                errors.append(f"{field} is required and must be a non-empty string")
        hypothesis_id = payload.get("hypothesis_id")
        if not _nonempty_string(hypothesis_id):
            errors.append("hypothesis_id is required and must reference the registry")
        elif hypothesis_id not in hypothesis_statuses:
            errors.append(f"unknown hypothesis_id: {hypothesis_id}")
        result_status = payload.get("result_status")
        if result_status not in RESULT_STATUSES:
            errors.append("result_status must be one of " + ", ".join(sorted(RESULT_STATUSES)))
        elif (
            result_status != "NO_RESULTS"
            and hypothesis_id in hypothesis_statuses
            and not hypothesis_statuses[hypothesis_id].startswith("FROZEN_")
        ):
            errors.append(
                "non-NO_RESULTS artifacts require a hypothesis status beginning FROZEN_"
            )
        metrics = payload.get("metrics", [])
        if not isinstance(metrics, list):
            errors.append("metrics must be a list when present")
            metrics = []
        charts = payload.get("charts", [])
        if not isinstance(charts, list):
            errors.append("charts must be a list when present")
            charts = []
        tables = payload.get("tables", [])
        if not isinstance(tables, list):
            errors.append("tables must be a list when present")
            tables = []

        entry = {
            "artifact": path.name,
            "status": "INVALID" if errors else "VALID",
            "validation_errors": errors,
            "schema_version": payload.get("schema_version"),
            "experiment_id": payload.get("experiment_id"),
            "hypothesis_id": hypothesis_id,
            "title": payload.get("title"),
            "result_status": result_status,
            "generated_at_utc": payload.get("generated_at_utc"),
            "summary": payload.get("summary"),
            "metrics": metrics,
            "charts": charts,
            "tables": tables,
        }
        experiments.append(entry)
        if errors:
            validation_errors.append({"artifact": path.name, "errors": errors})

    return {
        "schema_version": EXPERIMENTS_SCHEMA,
        "generated_at_utc": generated_at_utc,
        "experiments": experiments,
        "validation_errors": validation_errors,
        "valid_count": sum(item["status"] == "VALID" for item in experiments),
        "invalid_count": sum(item["status"] == "INVALID" for item in experiments),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(path)


def _write_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(text)
    temporary.replace(path)


def _standalone_workbench(payloads: dict[str, Any]) -> str:
    """Return an offline single-file rendering of the current workbench."""

    template = INDEX_PATH.read_text(encoding="utf-8")
    echarts = ECHARTS_PATH.read_text(encoding="utf-8")
    serialized = json.dumps(payloads, ensure_ascii=False, separators=(",", ":"))
    serialized = serialized.replace("<", "\\u003c").replace(">", "\\u003e")
    scripts = (
        f"<script>{echarts}</script>\n"
        f"<script>window.__WORKBENCH_DATA__={serialized};</script>"
    )
    marker = '<script src="/assets/echarts.min.js"></script>'
    if marker not in template:
        raise ValueError("workbench template missing local ECharts marker")
    return template.replace(marker, scripts, 1)


def build_workbench(
    repo_root: Path = REPO_ROOT,
    hypotheses_path: Path = HYPOTHESES_PATH,
    generated_at_utc: str | None = None,
) -> dict[str, Any]:
    """Build a frozen API snapshot; all writes are inside reports/workbench."""
    repo_root = Path(repo_root)
    manifest_path, experiments_dir, output_dir = _repo_paths(repo_root)
    generated_at_utc = generated_at_utc or _utc_now()
    hypotheses = load_hypotheses(Path(hypotheses_path))
    hypothesis_statuses = {
        item["hypothesis_id"]: item["status"] for item in hypotheses["hypotheses"]
    }
    coverage = aggregate_manifest(manifest_path, repo_root)
    liquidity = build_liquidity_snapshot(repo_root, generated_at_utc)
    main_events = build_main_events_snapshot(repo_root, generated_at_utc)
    experiments = discover_experiments(
        experiments_dir, hypothesis_statuses, generated_at_utc
    )
    overview = {
        "schema_version": OVERVIEW_SCHEMA,
        "generated_at_utc": generated_at_utc,
        "source_manifest": {
            key: coverage[key]
            for key in (
                "path",
                "sha256",
                "available",
                "total_manifest_rows",
                "matched_rows",
                "invalid_rows",
                "errors",
            )
        },
        "dates": coverage["dates"],
        "tables": coverage["tables"],
        "sports": coverage["sports"],
        "experiment_count": len(experiments["experiments"]),
        "valid_experiment_count": experiments["valid_count"],
        "invalid_experiment_count": experiments["invalid_count"],
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "overview.json", overview)
    _write_json(output_dir / "hypotheses.json", hypotheses)
    _write_json(output_dir / "experiments.json", experiments)
    _write_json(output_dir / "liquidity.json", liquidity)
    _write_json(output_dir / "main_events.json", main_events)
    _write_text(
        output_dir / "index.html",
        _standalone_workbench(
            {
                "overview": overview,
                "liquidity": liquidity,
                "main_events": main_events,
                "hypotheses": hypotheses,
                "experiments": experiments,
            }
        ),
    )
    return {
        "overview": overview,
        "hypotheses": hypotheses,
        "experiments": experiments,
        "liquidity": liquidity,
        "main_events": main_events,
    }


def is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_handler(
    index_path: Path = INDEX_PATH,
    report_dir: Path | None = None,
    echarts_path: Path = ECHARTS_PATH,
    intel_dir: Path | None = None,
):
    report_dir = report_dir or _repo_paths(REPO_ROOT)[2]
    intel_dir = intel_dir or (
        REPO_ROOT / "sandbox" / "research" / "reports" / "event_intel")
    resources = {
        "/api/overview": (report_dir / "overview.json", "application/json; charset=utf-8"),
        "/api/hypotheses": (report_dir / "hypotheses.json", "application/json; charset=utf-8"),
        "/api/experiments": (report_dir / "experiments.json", "application/json; charset=utf-8"),
        "/api/liquidity": (report_dir / "liquidity.json", "application/json; charset=utf-8"),
        "/api/main-events": (report_dir / "main_events.json", "application/json; charset=utf-8"),
        "/assets/echarts.min.js": (echarts_path, "text/javascript; charset=utf-8"),
    }

    class WorkbenchHandler(BaseHTTPRequestHandler):
        server_version = "ResearchWorkbench/1"

        def _headers(self, status: int, content_type: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self._headers(status, content_type, len(body))
            self.wfile.write(body)

        def _json_error(self, status: int, message: str) -> None:
            body = json.dumps({"error": message}).encode("utf-8")
            self._send(status, "application/json; charset=utf-8", body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            route = urlsplit(self.path).path
            if route in ("/", "/index.html"):
                path = index_path
                content_type = "text/html; charset=utf-8"
            elif route in resources:
                path, content_type = resources[route]
            elif route == "/intel" or route.startswith("/intel/"):
                # Event Intelligence Dashboard: safe static serving from the
                # generated artifact directory only (no path escape).
                rel = route[len("/intel"):].lstrip("/") or "index.html"
                candidate = (intel_dir / rel).resolve()
                intel_root = intel_dir.resolve()
                if intel_root not in candidate.parents and candidate != intel_root:
                    self._json_error(404, "not found")
                    return
                path = candidate
                suffix = candidate.suffix.lower()
                content_type = {
                    ".html": "text/html; charset=utf-8",
                    ".js": "text/javascript; charset=utf-8",
                    ".json": "application/json; charset=utf-8",
                }.get(suffix, "application/octet-stream")
            else:
                self._json_error(404, "not found")
                return
            try:
                body = path.read_bytes()
            except FileNotFoundError:
                self._json_error(503, "workbench snapshot not built")
                return
            except OSError:
                self._json_error(500, "unable to read workbench resource")
                return
            self._send(200, content_type, body)

        def _read_only(self) -> None:
            body = json.dumps({"error": "read-only server; only GET is allowed"}).encode("utf-8")
            self.send_response(405)
            self.send_header("Allow", "GET")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = _read_only
        do_PUT = _read_only
        do_PATCH = _read_only
        do_DELETE = _read_only

    return WorkbenchHandler


def serve(host: str = "127.0.0.1", port: int = 8791) -> None:
    if not is_loopback_host(host):
        raise ValueError("refusing non-loopback host; use 127.0.0.1, ::1, or localhost")
    server = ThreadingHTTPServer((host, port), make_handler())
    print(f"Research Workbench: http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("build", help="build the read-only API snapshot")
    for name, help_text in (
        ("serve", "serve an existing snapshot on loopback only"),
        ("run", "build, then serve on loopback only"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--host", default="127.0.0.1")
        command.add_argument("--port", type=int, default=8791)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command in {"build", "run"}:
        snapshot = build_workbench()
        print(
            json.dumps(
                {
                    "generated_at_utc": snapshot["overview"]["generated_at_utc"],
                    "sports": {
                        item["sport"]: item["total_rows"]
                        for item in snapshot["overview"]["sports"]
                    },
                    "experiments": snapshot["overview"]["experiment_count"],
                    "liquidity_available": snapshot["liquidity"]["available"],
                    "main_events_available": snapshot["main_events"]["available"],
                },
                sort_keys=True,
            )
        )
    if args.command in {"serve", "run"}:
        serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
