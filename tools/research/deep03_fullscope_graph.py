#!/usr/bin/env python3
"""Bounded market/family coverage graph for DEEP03-FULL-CHANNEL-01.

The graph is deliberately descriptive.  Canonical series/event/market links
are kept distinct from any heuristic root/game mapping, and input support is
scanned once per exact fact object with explicit row conservation.
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "deep03-fullscope-market-graph-v1"
METHOD_ID = "D3-FULL-MARKET-GRAPH-01"
FACT_CHANNELS = ("orderbooks_l1", "trades", "orderbooks_full")
CHANNEL_COLUMNS = {
    "orderbooks_l1": "l1_rows",
    "trades": "trade_rows",
    "orderbooks_full": "l2_rows",
}


class MarketGraphError(RuntimeError):
    """The exact graph input or a conservation invariant is invalid."""


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        + b"\n"
    )


def _quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _fact_relation(path: Path) -> str:
    """Route a fact object to its DuckDB reader by production file format.

    The warehouse contract (docs/warehouse_schema.md) ships orderbooks_l1 and
    orderbooks_full as .parquet and trades/settlements as .csv.gz.  Reading is
    routed exactly like the audited handler in deep03_v3_methods._relation_sql;
    an unknown extension is a refusal, never a guessed format.
    """
    name = path.name.lower()
    if name.endswith(".parquet"):
        return (
            f"read_parquet({_quote(path)},union_by_name=true,"
            "hive_partitioning=true)"
        )
    if name.endswith(".csv.gz") or name.endswith(".csv"):
        return (
            f"read_csv({_quote(path)},header=true,union_by_name=true,"
            "hive_partitioning=true,all_varchar=true)"
        )
    raise MarketGraphError(
        f"fact object has an unsupported file format (expected .parquet, "
        f".csv.gz or .csv): {path}"
    )


def _columns(con, relation: str) -> set[str]:
    return {str(row[0]) for row in con.execute(f"DESCRIBE {relation}").fetchall()}


def _source_expr(
    columns: set[str], candidates: Iterable[str], alias: str, sql_type: str
) -> str:
    present = [f'"{name}"' for name in candidates if name in columns]
    if not present:
        value = "NULL"
    elif len(present) == 1:
        value = present[0]
    else:
        value = "coalesce(" + ",".join(present) + ")"
    return f'try_cast({value} AS {sql_type}) AS "{alias}"'


def _objects(input_manifest: dict[str, Any], *, kind: str) -> list[dict[str, Any]]:
    rows = [
        row
        for row in input_manifest.get("objects") or []
        if row.get("kind") == kind
    ]
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("date") or ""),
            str(row.get("logical_key") or ""),
            str(row.get("source_version_id") or ""),
        ),
    )


def _dated_dim_objects(
    input_manifest: dict[str, Any], name: str
) -> list[dict[str, Any]]:
    suffix = f"/{name}.csv"
    return [
        row
        for row in _objects(input_manifest, kind="dim_snapshot")
        if str(row.get("logical_key") or "").endswith(suffix)
    ]


def _sports_fact_objects(input_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for row in _objects(input_manifest, kind="facts")
        if row.get("channel") in FACT_CHANNELS
        and "/category=Sports/" in str(row.get("logical_key") or "")
    ]


def _manifest_source_binding(input_manifest: dict[str, Any]) -> str:
    projection = [
        {
            "release_id": row.get("release_id"),
            "date": row.get("date"),
            "logical_key": row.get("logical_key"),
            "source_version_id": row.get("source_version_id"),
            "size": row.get("size"),
            "sha256": row.get("sha256"),
            "row_count": row.get("row_count"),
            "kind": row.get("kind"),
            "channel": row.get("channel"),
        }
        for row in sorted(
            input_manifest.get("objects") or [],
            key=lambda item: (
                str(item.get("release_id") or ""),
                str(item.get("logical_key") or ""),
                str(item.get("source_version_id") or ""),
            ),
        )
    ]
    return hashlib.sha256(_canonical_bytes(projection)).hexdigest()


def _create_dimension_tables(con) -> None:
    con.execute(
        "CREATE OR REPLACE TEMP TABLE fullscope_series_raw("
        "date DATE,series_ticker VARCHAR,title VARCHAR,category VARCHAR,"
        "fee_type VARCHAR,fee_multiplier VARCHAR)"
    )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE fullscope_events_raw("
        "date DATE,event_ticker VARCHAR,series_ticker VARCHAR,title VARCHAR,"
        "sub_title VARCHAR,category VARCHAR,mutually_exclusive BOOLEAN,"
        "updated_time TIMESTAMPTZ)"
    )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE fullscope_markets_raw("
        "date DATE,market_ticker VARCHAR,event_ticker VARCHAR,title VARCHAR,"
        "yes_sub_title VARCHAR,no_sub_title VARCHAR,market_type VARCHAR,"
        "event_structure VARCHAR,strike_type VARCHAR,bracket_rank VARCHAR,"
        "mve_collection_ticker VARCHAR,mve_selected_legs VARCHAR,"
        "occurrence_time TIMESTAMPTZ,close_time TIMESTAMPTZ,"
        "updated_time TIMESTAMPTZ)"
    )


def _load_dim_object(con, obj: dict[str, Any], family: str) -> int:
    path = Path(str(obj.get("local_path") or ""))
    if not path.is_file():
        raise MarketGraphError(f"dimension object is missing: {path}")
    date = str(obj.get("date") or "")
    if not date:
        raise MarketGraphError(f"dimension object has no bound date: {path}")
    view = "fullscope_dim_source"
    con.execute(
        f"CREATE OR REPLACE TEMP VIEW {view} AS "
        f"SELECT * FROM read_csv({_quote(path)},header=true,all_varchar=true)"
    )
    columns = _columns(con, view)
    if family == "series":
        required = {"ticker"}
        if not required.issubset(columns):
            raise MarketGraphError(f"series dimension identity missing: {path}")
        expressions = (
            _source_expr(columns, ("ticker", "series_ticker"), "series_ticker", "VARCHAR"),
            _source_expr(columns, ("title",), "title", "VARCHAR"),
            _source_expr(columns, ("category",), "category", "VARCHAR"),
            _source_expr(columns, ("fee_type",), "fee_type", "VARCHAR"),
            _source_expr(columns, ("fee_multiplier",), "fee_multiplier", "VARCHAR"),
        )
        con.execute(
            "INSERT INTO fullscope_series_raw SELECT DATE "
            + _quote(date)
            + ","
            + ",".join(expressions)
            + f" FROM {view}"
        )
    elif family == "events":
        if "event_ticker" not in columns:
            raise MarketGraphError(f"event dimension identity missing: {path}")
        expressions = (
            _source_expr(columns, ("event_ticker", "ticker"), "event_ticker", "VARCHAR"),
            _source_expr(columns, ("series_ticker",), "series_ticker", "VARCHAR"),
            _source_expr(columns, ("title",), "title", "VARCHAR"),
            _source_expr(columns, ("sub_title",), "sub_title", "VARCHAR"),
            _source_expr(columns, ("category",), "category", "VARCHAR"),
            _source_expr(columns, ("mutually_exclusive",), "mutually_exclusive", "BOOLEAN"),
            _source_expr(columns, ("last_updated_ts", "updated_time"), "updated_time", "TIMESTAMPTZ"),
        )
        con.execute(
            "INSERT INTO fullscope_events_raw SELECT DATE "
            + _quote(date)
            + ","
            + ",".join(expressions)
            + f" FROM {view}"
        )
    elif family == "markets":
        if not {"ticker", "event_ticker"}.issubset(columns):
            raise MarketGraphError(f"market dimension identity missing: {path}")
        expressions = (
            _source_expr(columns, ("ticker", "market_ticker"), "market_ticker", "VARCHAR"),
            _source_expr(columns, ("event_ticker",), "event_ticker", "VARCHAR"),
            _source_expr(columns, ("title",), "title", "VARCHAR"),
            _source_expr(columns, ("yes_sub_title",), "yes_sub_title", "VARCHAR"),
            _source_expr(columns, ("no_sub_title",), "no_sub_title", "VARCHAR"),
            _source_expr(columns, ("market_type",), "market_type", "VARCHAR"),
            _source_expr(columns, ("event_structure",), "event_structure", "VARCHAR"),
            _source_expr(columns, ("strike_type",), "strike_type", "VARCHAR"),
            _source_expr(columns, ("bracket_rank",), "bracket_rank", "VARCHAR"),
            _source_expr(columns, ("mve_collection_ticker",), "mve_collection_ticker", "VARCHAR"),
            _source_expr(columns, ("mve_selected_legs",), "mve_selected_legs", "VARCHAR"),
            _source_expr(columns, ("occurrence_datetime",), "occurrence_time", "TIMESTAMPTZ"),
            _source_expr(columns, ("close_time", "expected_expiration_time"), "close_time", "TIMESTAMPTZ"),
            _source_expr(columns, ("updated_time",), "updated_time", "TIMESTAMPTZ"),
        )
        con.execute(
            "INSERT INTO fullscope_markets_raw SELECT DATE "
            + _quote(date)
            + ","
            + ",".join(expressions)
            + f" FROM {view}"
        )
    else:  # pragma: no cover - internal caller fixes the finite family set.
        raise MarketGraphError(f"unknown dimension family: {family}")
    return int(con.execute(f"SELECT count(*) FROM {view}").fetchone()[0])


def _load_dimensions(con, input_manifest: dict[str, Any]) -> dict[str, Any]:
    _create_dimension_tables(con)
    counts: dict[str, int] = {}
    for family in ("series", "events", "markets"):
        objects = _dated_dim_objects(input_manifest, family)
        if not objects:
            raise MarketGraphError(f"no exact dated {family} dimensions")
        total = 0
        for obj in objects:
            total += _load_dim_object(con, obj, family)
        counts[family] = total
        counts[f"{family}_objects"] = len(objects)
    return counts


def _create_support_table(con) -> None:
    con.execute(
        "CREATE OR REPLACE TEMP TABLE fullscope_fact_support("
        "date DATE,market_ticker VARCHAR,l1_rows UBIGINT,trade_rows UBIGINT,"
        "l2_rows UBIGINT)"
    )


def _scan_fact_support(con, input_manifest: dict[str, Any]) -> dict[str, Any]:
    _create_support_table(con)
    source_rows: dict[str, int] = defaultdict(int)
    source_objects: dict[str, int] = defaultdict(int)
    for obj in _sports_fact_objects(input_manifest):
        channel = str(obj["channel"])
        path = Path(str(obj.get("local_path") or ""))
        if not path.is_file():
            raise MarketGraphError(f"fact object is missing: {path}")
        con.execute(
            "CREATE OR REPLACE TEMP VIEW fullscope_fact_source AS "
            f"SELECT * FROM {_fact_relation(path)}"
        )
        columns = _columns(con, "fullscope_fact_source")
        ticker = "market_ticker" if "market_ticker" in columns else (
            "ticker" if "ticker" in columns else None
        )
        if ticker is None:
            raise MarketGraphError(f"fact market identity missing: {path}")
        actual = int(con.execute("SELECT count(*) FROM fullscope_fact_source").fetchone()[0])
        declared = obj.get("row_count")
        # The manifest is the row authority for every fact object, in the
        # standalone graph exactly as in the integrated runner: a missing or
        # non-exact declared count is a refusal, never a silent scan.
        if type(declared) is not int or declared < 0:
            raise MarketGraphError(
                "fact row_count is mandatory and must be an exact nonnegative"
                f" integer: {obj.get('logical_key')}: declared={declared!r}"
            )
        if declared != actual:
            raise MarketGraphError(
                f"fact row conservation failed: {obj.get('logical_key')}:"
                f" declared={declared} actual={actual}"
            )
        date = str(obj.get("date") or "")
        target = CHANNEL_COLUMNS[channel]
        values = {name: "0::UBIGINT" for name in CHANNEL_COLUMNS.values()}
        values[target] = "count(*)::UBIGINT"
        con.execute(
            "INSERT INTO fullscope_fact_support "
            f"SELECT DATE {_quote(date)},try_cast(\"{ticker}\" AS VARCHAR),"
            f"{values['l1_rows']},{values['trade_rows']},{values['l2_rows']} "
            "FROM fullscope_fact_source "
            f"WHERE try_cast(\"{ticker}\" AS VARCHAR) IS NOT NULL GROUP BY 2"
        )
        source_rows[channel] += actual
        source_objects[channel] += 1
    con.execute(
        "CREATE OR REPLACE TEMP TABLE fullscope_support AS "
        "SELECT date,market_ticker,sum(l1_rows)::UBIGINT AS l1_rows,"
        "sum(trade_rows)::UBIGINT AS trade_rows,sum(l2_rows)::UBIGINT AS l2_rows "
        "FROM fullscope_fact_support GROUP BY date,market_ticker"
    )
    for channel, column in CHANNEL_COLUMNS.items():
        got = int(con.execute(f"SELECT coalesce(sum({column}),0) FROM fullscope_support").fetchone()[0])
        if got != source_rows[channel]:
            raise MarketGraphError(
                f"support conservation failed for {channel}: source={source_rows[channel]} graph={got}"
            )
    return {
        "source_rows": dict(sorted(source_rows.items())),
        "source_objects": dict(sorted(source_objects.items())),
        "declared_row_count_authority": "REQUIRED_EXACT_MATCH_EVERY_FACT_OBJECT",
    }


def _build_graph_tables(con) -> None:
    con.execute(
        "CREATE OR REPLACE TEMP TABLE fullscope_series_qc AS "
        "SELECT date,series_ticker,count(*) AS raw_rows,"
        "count(DISTINCT hash(struct_pack(title:=title,category:=category,"
        "fee_type:=fee_type,fee_multiplier:=fee_multiplier))) AS variants "
        "FROM fullscope_series_raw WHERE series_ticker IS NOT NULL GROUP BY 1,2"
    )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE fullscope_event_qc AS "
        "SELECT date,event_ticker,count(*) AS raw_rows,"
        "count(DISTINCT hash(struct_pack(series_ticker:=series_ticker,title:=title,"
        "sub_title:=sub_title,category:=category,"
        "mutually_exclusive:=mutually_exclusive))) AS variants "
        "FROM fullscope_events_raw WHERE event_ticker IS NOT NULL GROUP BY 1,2"
    )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE fullscope_market_qc AS "
        "SELECT date,market_ticker,count(*) AS raw_rows,"
        "count(DISTINCT hash(struct_pack(event_ticker:=event_ticker,title:=title,"
        "yes_sub_title:=yes_sub_title,no_sub_title:=no_sub_title,"
        "market_type:=market_type,event_structure:=event_structure,"
        "strike_type:=strike_type,bracket_rank:=bracket_rank,"
        "mve_collection_ticker:=mve_collection_ticker,"
        "mve_selected_legs:=mve_selected_legs))) AS variants "
        "FROM fullscope_markets_raw WHERE market_ticker IS NOT NULL GROUP BY 1,2"
    )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE fullscope_series AS "
        "SELECT r.date,r.series_ticker,min(r.title) AS series_title,"
        "min(r.category) AS series_category,min(r.fee_type) AS fee_type,"
        "min(r.fee_multiplier) AS fee_multiplier "
        "FROM fullscope_series_raw r JOIN fullscope_series_qc q USING(date,series_ticker) "
        "WHERE q.variants=1 GROUP BY 1,2"
    )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE fullscope_events AS "
        "SELECT r.date,r.event_ticker,min(r.series_ticker) AS series_ticker,"
        "min(r.title) AS event_title,min(r.sub_title) AS event_sub_title,"
        "min(r.category) AS event_category,min(r.mutually_exclusive) AS mutually_exclusive,"
        "min(r.updated_time) AS event_updated_time "
        "FROM fullscope_events_raw r JOIN fullscope_event_qc q USING(date,event_ticker) "
        "WHERE q.variants=1 GROUP BY 1,2"
    )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE fullscope_markets AS "
        "SELECT r.date,r.market_ticker,min(r.event_ticker) AS event_ticker,"
        "min(r.title) AS market_title,min(r.yes_sub_title) AS yes_sub_title,"
        "min(r.no_sub_title) AS no_sub_title,min(r.market_type) AS market_type,"
        "min(r.event_structure) AS event_structure,min(r.strike_type) AS strike_type,"
        "min(r.bracket_rank) AS bracket_rank,"
        "min(r.mve_collection_ticker) AS mve_collection_ticker,"
        "min(r.mve_selected_legs) AS mve_selected_legs,"
        "min(r.occurrence_time) AS occurrence_time,min(r.close_time) AS close_time,"
        "min(r.updated_time) AS market_updated_time "
        "FROM fullscope_markets_raw r JOIN fullscope_market_qc q USING(date,market_ticker) "
        "WHERE q.variants=1 GROUP BY 1,2"
    )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE fullscope_graph AS "
        "WITH universe AS ("
        " SELECT date,market_ticker FROM fullscope_support"
        " UNION SELECT m.date,m.market_ticker FROM fullscope_markets m "
        " LEFT JOIN fullscope_events e USING(date,event_ticker) "
        " LEFT JOIN fullscope_series s ON s.date=m.date AND s.series_ticker=e.series_ticker "
        " WHERE lower(coalesce(e.event_category,s.series_category,''))='sports'"
        ") SELECT u.date,u.market_ticker,m.event_ticker,e.series_ticker,"
        "s.series_category,e.event_category,s.fee_type,s.fee_multiplier,"
        "e.event_title,e.event_sub_title,e.mutually_exclusive,"
        "m.market_title,m.yes_sub_title,m.no_sub_title,m.market_type,"
        "m.event_structure,m.strike_type,m.bracket_rank,"
        "m.mve_collection_ticker,m.mve_selected_legs,m.occurrence_time,m.close_time,"
        "coalesce(p.l1_rows,0)::UBIGINT AS l1_rows,"
        "coalesce(p.trade_rows,0)::UBIGINT AS trade_rows,"
        "coalesce(p.l2_rows,0)::UBIGINT AS l2_rows,"
        "CASE WHEN mq.variants>1 THEN 'AMBIGUOUS_MARKET' "
        "WHEN m.market_ticker IS NULL THEN 'MISSING_MARKET_DIM' "
        "WHEN eq.variants>1 THEN 'AMBIGUOUS_EVENT' "
        "WHEN e.event_ticker IS NULL THEN 'MISSING_EVENT_DIM' "
        "WHEN sq.variants>1 THEN 'AMBIGUOUS_SERIES' "
        "WHEN s.series_ticker IS NULL THEN 'MISSING_SERIES_DIM' "
        "ELSE 'CANONICAL_FAMILY_EVENT' END AS mapping_status,"
        "CASE WHEN m.mve_collection_ticker IS NOT NULL OR m.mve_selected_legs IS NOT NULL "
        "THEN 'MVE_METADATA_PRESENT' ELSE 'CLOB_OR_UNKNOWN' END AS execution_surface "
        "FROM universe u LEFT JOIN fullscope_support p USING(date,market_ticker) "
        "LEFT JOIN fullscope_market_qc mq USING(date,market_ticker) "
        "LEFT JOIN fullscope_markets m USING(date,market_ticker) "
        "LEFT JOIN fullscope_event_qc eq ON eq.date=m.date AND eq.event_ticker=m.event_ticker "
        "LEFT JOIN fullscope_events e ON e.date=m.date AND e.event_ticker=m.event_ticker "
        "LEFT JOIN fullscope_series_qc sq ON sq.date=e.date AND sq.series_ticker=e.series_ticker "
        "LEFT JOIN fullscope_series s ON s.date=e.date AND s.series_ticker=e.series_ticker"
    )


def _rows(con, sql: str) -> list[dict[str, Any]]:
    cursor = con.execute(sql)
    names = [str(item[0]) for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def build_market_graph(
    con,
    input_manifest: dict[str, Any],
    *,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Build the exact Sports market graph and optionally persist its tables."""
    dimension_counts = _load_dimensions(con, input_manifest)
    support = _scan_fact_support(con, input_manifest)
    _build_graph_tables(con)
    graph_rows = int(con.execute("SELECT count(*) FROM fullscope_graph").fetchone()[0])
    if graph_rows <= 0:
        raise MarketGraphError("market graph is empty")
    l2_dates = [
        str(row[0])
        for row in con.execute(
            "SELECT DISTINCT date FROM fullscope_graph WHERE l2_rows>0 ORDER BY date"
        ).fetchall()
    ]
    release_dates = [str(value) for value in input_manifest.get("release_dates") or []]
    missing_l2_dates = sorted(set(release_dates) - set(l2_dates))
    mapping_status = _rows(
        con,
        "SELECT mapping_status,count(*) AS market_days,"
        "count(DISTINCT market_ticker) AS markets,"
        "sum(l1_rows) AS l1_rows,sum(trade_rows) AS trade_rows,sum(l2_rows) AS l2_rows "
        "FROM fullscope_graph GROUP BY 1 ORDER BY 1",
    )
    channel_by_date = _rows(
        con,
        "SELECT cast(date AS VARCHAR) AS date,count(*) AS market_days,"
        "sum(l1_rows) AS l1_rows,"
        "sum(trade_rows) AS trade_rows,sum(l2_rows) AS l2_rows,"
        "count(*) FILTER (WHERE l1_rows>0) AS l1_markets,"
        "count(*) FILTER (WHERE trade_rows>0) AS trade_markets,"
        "count(*) FILTER (WHERE l2_rows>0) AS l2_markets "
        "FROM fullscope_graph GROUP BY 1 ORDER BY 1",
    )
    catalog_versions: dict[str, set[str]] = defaultdict(set)
    for obj in _objects(input_manifest, kind="catalog"):
        catalog_versions[str(obj.get("logical_key"))].add(str(obj.get("sha256")))
    result = {
        "schema_version": SCHEMA,
        "method_id": METHOD_ID,
        "status": "EXECUTED_DESCRIPTIVE_ONLY",
        "source_binding_sha256": _manifest_source_binding(input_manifest),
        "release_dates": release_dates,
        "dimension_counts": dimension_counts,
        "support_conservation": support,
        "graph_market_days": graph_rows,
        "l2_present_dates": l2_dates,
        "l2_absent_dates": missing_l2_dates,
        "mapping_status": mapping_status,
        "channel_by_date": channel_by_date,
        "catalog_inventory": {
            "logical_keys": len(catalog_versions),
            "content_versions_by_key": {
                key: sorted(values) for key, values in sorted(catalog_versions.items())
            },
        },
        "claims": {
            "canonical_links": "series -> event -> market only",
            "root_mapping": "NOT_ATTEMPTED; no heuristic game root is promoted",
            "payout_exhaustiveness": False,
            "arbitrage_or_pnl": False,
        },
    }
    if output_dir is not None:
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        graph_path = output / "MARKET_GRAPH.parquet"
        summary_path = output / "MARKET_GRAPH_RECEIPT.json"
        con.execute(
            "COPY (SELECT * FROM fullscope_graph ORDER BY date,market_ticker) "
            f"TO {_quote(graph_path)} (FORMAT PARQUET,COMPRESSION ZSTD)"
        )
        summary_path.write_bytes(_canonical_bytes(result))
        result["artifacts"] = {
            "market_graph": {
                "path": str(graph_path),
                "bytes": graph_path.stat().st_size,
                "sha256": hashlib.sha256(graph_path.read_bytes()).hexdigest(),
            },
            "receipt": {
                "path": str(summary_path),
                "bytes": summary_path.stat().st_size,
                "sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
            },
        }
    return result
