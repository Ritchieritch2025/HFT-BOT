#!/usr/bin/env python3
"""Corrected D3-W2A descriptive methods for exact V3 Sports facts.

The four methods are intentionally narrow:

* D3-B01-MARKOUT uses only receive-clock-past books and strictly future books;
* D3-B02-ONESIDE ends state at update/TTL/gap/terminal and right-censors tails;
* D3-B03-XMKT refuses a residual without exhaustive payouts, fees and fills;
* D3-B04-RHYTHM uses active market-minute/event-minute denominators.

No method computes strategy PnL or emits a candidate verdict.
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable


METHODS = (
    "D3-B01-MARKOUT",
    "D3-B02-ONESIDE",
    "D3-B03-XMKT",
    "D3-B04-RHYTHM",
)
HORIZONS_US = (1_000_000, 5_000_000, 30_000_000)
MAX_BOOK_AGE_US = 5_000_000
BOOK_TTL_US = 60_000_000
L1_INTERVAL_COLUMNS = (
    "date",
    "t_us",
    "market_ticker",
    "event_proxy",
    "sport",
    "book_state",
    "right_censored",
    "starts_in_gap",
    "interval_end_us",
    "duration_us",
    "phase",
)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def rows_as_dicts(con, sql: str) -> list[dict[str, Any]]:
    cursor = con.execute(sql)
    names = [item[0] for item in cursor.description]
    return [
        {name: _json_value(value) for name, value in zip(names, row)}
        for row in cursor.fetchall()
    ]


def scalar(con, sql: str, default: Any = 0) -> Any:
    row = con.execute(sql).fetchone()
    return default if not row or row[0] is None else _json_value(row[0])


def quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def path_list(paths: Iterable[Path]) -> str:
    values = list(paths)
    if not values:
        raise ValueError("empty source path list")
    return "[" + ",".join(quote(path) for path in values) + "]"


def _relation_sql(paths: list[Path]) -> str:
    parquet = [path for path in paths if path.name.endswith(".parquet")]
    csvs = [path for path in paths if not path.name.endswith(".parquet")]
    parts: list[str] = []
    if parquet:
        parts.append(
            "SELECT * FROM read_parquet(%s,union_by_name=true,"
            "hive_partitioning=true)" % path_list(parquet)
        )
    if csvs:
        parts.append(
            "SELECT * FROM read_csv(%s,header=true,union_by_name=true,"
            "hive_partitioning=true,all_varchar=true)" % path_list(csvs)
        )
    if not parts:
        raise ValueError("no readable fact files")
    return " UNION ALL BY NAME ".join(parts)


def _columns(con, relation: str) -> set[str]:
    return {
        row[0]
        for row in con.execute(
            f"DESCRIBE SELECT * FROM {relation}"
        ).fetchall()
    }


def _expr(
    columns: set[str], candidates: tuple[str, ...], alias: str, sql_type: str
) -> str:
    available = [f'"{name}"' for name in candidates if name in columns]
    if not available:
        base = "NULL"
    elif len(available) == 1:
        base = available[0]
    else:
        base = "coalesce(" + ",".join(available) + ")"
    return f'try_cast({base} AS {sql_type}) AS "{alias}"'


def _fact_objects(input_manifest: dict[str, Any], channel: str) -> list[dict[str, Any]]:
    return [
        obj
        for obj in input_manifest.get("objects") or []
        if obj.get("kind") == "facts"
        and obj.get("channel") == channel
        and "/category=Sports/" in str(obj.get("logical_key") or "")
    ]


def _kind_objects(input_manifest: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [
        obj
        for obj in input_manifest.get("objects") or []
        if obj.get("kind") == kind
    ]


def _object_provenance(
    input_manifest: dict[str, Any], *, channels: set[str], kinds: set[str] | None = None
) -> list[dict[str, Any]]:
    kinds = kinds or set()
    rows = []
    for obj in input_manifest.get("objects") or []:
        if obj.get("channel") not in channels and obj.get("kind") not in kinds:
            continue
        rows.append(
            {
                "release_id": obj["release_id"],
                "logical_key": obj["logical_key"],
                "source_version_id": obj["source_version_id"],
                "sha256": obj["sha256"],
                "row_count": obj.get("row_count"),
            }
        )
    return rows


def _phase_sql(alias: str) -> str:
    return (
        f"CASE WHEN {alias}.occurrence_us IS NULL THEN 'UNKNOWN_PHASE' "
        f"WHEN {alias}.t_us < {alias}.occurrence_us THEN 'PRE_SCHEDULED_START' "
        "ELSE 'POST_SCHEDULED_START_PROXY' END"
    )


def _mid_logodds_sql(bid: str, ask: str) -> str:
    def logit(column: str) -> str:
        clipped = f"greatest(1.0,least(9999.0,cast({column} AS DOUBLE)))"
        return f"ln(({clipped})/(10000.0-({clipped})))"

    return f"(({logit(bid)})+({logit(ask)}))/2.0"


def _spread_logodds_sql(bid: str, ask: str) -> str:
    def logit(column: str) -> str:
        clipped = f"greatest(1.0,least(9999.0,cast({column} AS DOUBLE)))"
        return f"ln(({clipped})/(10000.0-({clipped})))"

    return f"({logit(ask)})-({logit(bid)})"


def _canonical_date(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Deep03 release date must be a canonical ISO string")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid Deep03 release date: {value!r}") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"non-canonical Deep03 release date: {value!r}")
    return value


def _manifest_release_dates(input_manifest: dict[str, Any]) -> tuple[str, ...]:
    """Return the manifest-bound dates in deterministic chronological order."""
    raw_dates = input_manifest.get("release_dates")
    if not isinstance(raw_dates, list) or not raw_dates:
        raise ValueError("INPUT_MANIFEST release_dates is missing or empty")
    dates = tuple(_canonical_date(value) for value in raw_dates)
    if len(dates) != len(set(dates)):
        raise ValueError("INPUT_MANIFEST release_dates contains a duplicate date")

    releases = input_manifest.get("releases")
    if not isinstance(releases, list) or len(releases) != len(dates):
        raise ValueError("INPUT_MANIFEST release summaries do not bind release_dates")
    summary_dates = tuple(
        _canonical_date(release.get("date"))
        for release in releases
        if isinstance(release, dict)
    )
    if len(summary_dates) != len(dates) or set(summary_dates) != set(dates):
        raise ValueError("INPUT_MANIFEST release summary dates differ from release_dates")

    l1_dates = {
        _canonical_date(obj.get("date"))
        for obj in _fact_objects(input_manifest, "orderbooks_l1")
    }
    if not l1_dates.issubset(set(dates)):
        raise ValueError("L1 object date falls outside INPUT_MANIFEST release_dates")
    return tuple(sorted(dates))


def _l1_interval_select_sql(exact_date: str | None) -> str:
    """Build the unchanged interval estimand, optionally bounded to one date."""
    date_filter = ""
    if exact_date is not None:
        date_filter = f"l.date=DATE {quote(_canonical_date(exact_date))} AND "
    return f"""
        WITH ordered AS (
          -- Keep the exact causal ordering keys in the window, but do not
          -- carry the full L1 row through its blocking sort.  The exact-date
          -- predicate makes each production sort one manifest day wide.
          SELECT date,t_us,market_ticker,event_proxy,sport,book_state,
                 occurrence_us,close_us,
                 lead(t_us) OVER (
                   PARTITION BY date,market_ticker
                   ORDER BY t_us,coalesce(recv_wall_ns,0),
                            coalesce(recv_mono_ns,0),coalesce(ws_sid,0),
                            coalesce(ws_seq,0)
                 ) AS next_t_us
          FROM l1_enriched l
          WHERE {date_filter}date IS NOT NULL AND market_ticker IS NOT NULL
                AND t_us IS NOT NULL
        ), bounded AS (
          SELECT date,t_us,market_ticker,event_proxy,sport,book_state,
                 occurrence_us,next_t_us IS NULL AS right_censored,
                 CASE WHEN next_t_us IS NULL THEN t_us ELSE
                   least(next_t_us,t_us+{BOOK_TTL_US},
                     CASE WHEN close_us>t_us THEN close_us
                          ELSE t_us+{BOOK_TTL_US} END)
                 END AS base_end_us
          FROM ordered
        ), gap_bound AS (
          SELECT b.date,b.t_us,b.market_ticker,b.event_proxy,b.sport,
                 b.book_state,b.occurrence_us,b.right_censored,b.base_end_us,
                 (SELECT min(g.start_us) FROM capture_gaps g
                  WHERE g.date=b.date AND g.start_us>b.t_us
                    AND g.start_us<b.base_end_us) AS next_gap_start_us,
                 EXISTS(SELECT 1 FROM capture_gaps g
                        WHERE g.date=b.date AND g.start_us<=b.t_us
                          AND g.end_us>b.t_us) AS starts_in_gap
          FROM bounded b
        )
        SELECT date,t_us,market_ticker,event_proxy,sport,book_state,
               right_censored,starts_in_gap,
               least(base_end_us,coalesce(next_gap_start_us,base_end_us))
                 AS interval_end_us,
               CASE WHEN right_censored OR starts_in_gap THEN 0
                    ELSE greatest(0,least(base_end_us,
                         coalesce(next_gap_start_us,base_end_us))-t_us)
               END AS duration_us,
               {_phase_sql('gap_bound')} AS phase
        FROM gap_bound
    """


def _materialize_l1_intervals(con, dates: tuple[str, ...]) -> None:
    """Materialize fixed-width L1 intervals with one bounded sort per date."""
    bounded_dates = tuple(sorted(_canonical_date(date) for date in dates))
    if len(bounded_dates) != len(set(bounded_dates)):
        raise ValueError("L1 interval materialization received duplicate dates")
    con.execute(
        "CREATE OR REPLACE TEMP TABLE l1_intervals("
        "date DATE,t_us BIGINT,market_ticker VARCHAR,event_proxy VARCHAR,"
        "sport VARCHAR,book_state VARCHAR,right_censored BOOLEAN,"
        "starts_in_gap BOOLEAN,interval_end_us BIGINT,duration_us BIGINT,"
        "phase VARCHAR)"
    )
    columns = ",".join(L1_INTERVAL_COLUMNS)
    for date in bounded_dates:
        print(
            f"D3_W2A_STAGE l1_intervals date={date} state=START",
            flush=True,
        )
        con.execute(
            f"INSERT INTO l1_intervals({columns}) "
            + _l1_interval_select_sql(date)
        )
        print(
            f"D3_W2A_STAGE l1_intervals date={date} state=COMPLETE",
            flush=True,
        )


def setup_database(con, input_manifest: dict[str, Any]) -> dict[str, Any]:
    """Create normalized receive-clock views and return explicit capabilities."""
    capabilities: dict[str, Any] = {
        "l1": {"present": False, "columns": []},
        "trades": {"present": False, "columns": []},
        "dim_markets": {"present": False, "columns": []},
        "capture_gaps": 0,
    }

    con.execute(
        "CREATE OR REPLACE TEMP TABLE capture_gaps("
        "date DATE,start_us BIGINT,end_us BIGINT)"
    )
    for obj in _kind_objects(input_manifest, "capture_gaps_projection"):
        path = Path(obj["local_path"])
        date = str(obj["date"])
        con.execute(
            "INSERT INTO capture_gaps SELECT DATE %s,try_cast(start_us AS BIGINT),"
            "try_cast(end_us AS BIGINT) FROM read_csv(%s,header=true,all_varchar=true) "
            "WHERE try_cast(start_us AS BIGINT) IS NOT NULL "
            "AND try_cast(end_us AS BIGINT)>try_cast(start_us AS BIGINT)"
            % (quote(date), quote(path))
        )
    capabilities["capture_gaps"] = int(
        scalar(con, "SELECT count(*) FROM capture_gaps")
    )

    dim_objects = [
        obj
        for obj in _kind_objects(input_manifest, "dim_snapshot")
        if str(obj.get("logical_key") or "").endswith("/markets.csv")
    ]
    con.execute(
        "CREATE OR REPLACE TEMP TABLE dim_market("
        "date DATE,market_ticker VARCHAR,event_ticker VARCHAR,"
        "occurrence_us BIGINT,close_us BIGINT)"
    )
    if dim_objects:
        dim_paths = [Path(obj["local_path"]) for obj in dim_objects]
        con.execute(
            "CREATE OR REPLACE TEMP VIEW dim_market_source AS "
            + _relation_sql(dim_paths)
        )
        columns = _columns(con, "dim_market_source")
        capabilities["dim_markets"] = {
            "present": True,
            "columns": sorted(columns),
        }
        ticker = _expr(columns, ("ticker", "market_ticker"), "market_ticker", "VARCHAR")
        event = _expr(columns, ("event_ticker",), "event_ticker", "VARCHAR")
        date = _expr(columns, ("date",), "date", "DATE")
        occurrence = _expr(
            columns,
            ("occurrence_datetime",),
            "occurrence_datetime",
            "TIMESTAMPTZ",
        )
        close = _expr(
            columns,
            ("close_time", "close", "expected_expiration_time"),
            "close_time",
            "TIMESTAMPTZ",
        )
        con.execute(
            "CREATE OR REPLACE TEMP VIEW dim_market_typed AS SELECT "
            + ",".join((date, ticker, event, occurrence, close))
            + " FROM dim_market_source"
        )
        con.execute(
            "INSERT INTO dim_market "
            "SELECT date,market_ticker,min(event_ticker),"
            "min(epoch_us(occurrence_datetime)),min(epoch_us(close_time)) "
            "FROM dim_market_typed WHERE date IS NOT NULL "
            "AND market_ticker IS NOT NULL GROUP BY date,market_ticker"
        )

    l1_objects = _fact_objects(input_manifest, "orderbooks_l1")
    if l1_objects:
        release_dates = _manifest_release_dates(input_manifest)
        l1_paths = [Path(obj["local_path"]) for obj in l1_objects]
        con.execute(
            "CREATE OR REPLACE TEMP VIEW l1_source AS " + _relation_sql(l1_paths)
        )
        columns = _columns(con, "l1_source")
        capabilities["l1"] = {"present": True, "columns": sorted(columns)}
        projections = (
            _expr(columns, ("date",), "date", "DATE"),
            _expr(columns, ("local_recv_ts_us",), "t_us", "BIGINT"),
            _expr(columns, ("recv_wall_ns",), "recv_wall_ns", "BIGINT"),
            _expr(columns, ("recv_mono_ns",), "recv_mono_ns", "BIGINT"),
            _expr(columns, ("ws_sid",), "ws_sid", "BIGINT"),
            _expr(columns, ("ws_seq",), "ws_seq", "BIGINT"),
            _expr(columns, ("market_ticker", "ticker"), "market_ticker", "VARCHAR"),
            _expr(columns, ("event_ticker",), "fact_event_ticker", "VARCHAR"),
            _expr(columns, ("series_ticker",), "series_ticker", "VARCHAR"),
            _expr(columns, ("subcategory", "sport"), "sport", "VARCHAR"),
            _expr(columns, ("group", "league"), "league", "VARCHAR"),
            _expr(columns, ("yes_bid_e4",), "yes_bid_e4", "BIGINT"),
            _expr(columns, ("yes_ask_e4",), "yes_ask_e4", "BIGINT"),
            _expr(columns, ("yes_bid_qty_e4",), "yes_bid_qty_e4", "BIGINT"),
            _expr(columns, ("yes_ask_qty_e4",), "yes_ask_qty_e4", "BIGINT"),
        )
        con.execute(
            "CREATE OR REPLACE TEMP VIEW l1_norm AS SELECT "
            + ",".join(projections)
            + " FROM l1_source"
        )
        con.execute(
            "CREATE OR REPLACE TEMP VIEW l1_enriched AS "
            "SELECT l.*,coalesce(l.fact_event_ticker,d.event_ticker,l.market_ticker) "
            "AS event_proxy,d.occurrence_us,d.close_us,"
            "CASE WHEN l.yes_bid_e4>0 AND l.yes_bid_e4<10000 "
            "AND l.yes_ask_e4>0 AND l.yes_ask_e4<10000 "
            "AND l.yes_bid_e4<l.yes_ask_e4 THEN 'TWO_SIDED' "
            "WHEN (coalesce(l.yes_bid_e4>0 AND l.yes_bid_e4<10000,false) <> "
            "coalesce(l.yes_ask_e4>0 AND l.yes_ask_e4<10000,false)) "
            "THEN 'ONE_SIDED' "
            "ELSE 'INVALID_BOOK' END AS book_state "
            "FROM l1_norm l LEFT JOIN dim_market d USING(date,market_ticker)"
        )
        _materialize_l1_intervals(con, release_dates)

    trade_objects = _fact_objects(input_manifest, "trades")
    if trade_objects:
        trade_paths = [Path(obj["local_path"]) for obj in trade_objects]
        con.execute(
            "CREATE OR REPLACE TEMP VIEW trades_source AS "
            + _relation_sql(trade_paths)
        )
        columns = _columns(con, "trades_source")
        capabilities["trades"] = {"present": True, "columns": sorted(columns)}
        projections = (
            _expr(columns, ("date",), "date", "DATE"),
            _expr(columns, ("local_recv_ts_us",), "t_us", "BIGINT"),
            _expr(columns, ("recv_wall_ns",), "recv_wall_ns", "BIGINT"),
            _expr(columns, ("recv_mono_ns",), "recv_mono_ns", "BIGINT"),
            _expr(columns, ("market_ticker", "ticker"), "market_ticker", "VARCHAR"),
            _expr(columns, ("event_ticker",), "fact_event_ticker", "VARCHAR"),
            _expr(columns, ("series_ticker",), "series_ticker", "VARCHAR"),
            _expr(columns, ("subcategory", "sport"), "sport", "VARCHAR"),
            _expr(columns, ("group", "league"), "league", "VARCHAR"),
            _expr(columns, ("trade_id",), "trade_id", "VARCHAR"),
            _expr(columns, ("yes_price_e4",), "yes_price_e4", "BIGINT"),
            _expr(columns, ("no_price_e4",), "no_price_e4", "BIGINT"),
            _expr(columns, ("count_e4",), "count_e4", "BIGINT"),
            _expr(columns, ("taker_side",), "taker_side", "VARCHAR"),
        )
        con.execute(
            "CREATE OR REPLACE TEMP VIEW trades_norm AS SELECT "
            + ",".join(projections)
            + " FROM trades_source"
        )
        print("D3_W2A_STAGE trade_id_qc state=START", flush=True)
        con.execute(
            """
            CREATE OR REPLACE TEMP TABLE trade_id_qc AS
            SELECT trade_id,count(*) AS raw_rows,
                   count(DISTINCT hash(struct_pack(
                     date:=date,t_us:=t_us,market_ticker:=market_ticker,
                     yes_price_e4:=yes_price_e4,no_price_e4:=no_price_e4,
                     count_e4:=count_e4,taker_side:=lower(taker_side)
                   ))) AS economic_variants
            FROM trades_norm WHERE trade_id IS NOT NULL GROUP BY trade_id
            """
        )
        print("D3_W2A_STAGE trade_id_qc state=COMPLETE", flush=True)
        print("D3_W2A_STAGE trades_dedup state=START", flush=True)
        con.execute(
            """
            CREATE OR REPLACE TEMP TABLE trades_dedup AS
            SELECT * EXCLUDE(rn) FROM (
              SELECT t.*,coalesce(t.fact_event_ticker,d.event_ticker,
                     t.market_ticker) AS event_proxy,d.occurrence_us,
                     row_number() OVER (
                       PARTITION BY t.trade_id
                       ORDER BY t.t_us,coalesce(t.recv_wall_ns,0),
                                coalesce(t.recv_mono_ns,0),t.market_ticker
                     ) AS rn
              FROM trades_norm t JOIN trade_id_qc q USING(trade_id)
              LEFT JOIN dim_market d USING(date,market_ticker)
              WHERE q.economic_variants=1 AND t.date IS NOT NULL
                AND t.t_us IS NOT NULL AND t.market_ticker IS NOT NULL
            ) WHERE rn=1
            """
        )
        print("D3_W2A_STAGE trades_dedup state=COMPLETE", flush=True)
        con.execute(
            """
            CREATE OR REPLACE TEMP VIEW trades_clean AS
            SELECT *,CASE WHEN lower(taker_side)='yes' THEN 1
                          WHEN lower(taker_side)='no' THEN -1 END AS taker_sign
            FROM trades_dedup
            WHERE yes_price_e4 BETWEEN 1 AND 9999
              AND lower(taker_side) IN ('yes','no')
            """
        )
    return capabilities


def _base_result(
    method_id: str,
    title: str,
    input_manifest: dict[str, Any],
    channels: set[str],
) -> dict[str, Any]:
    return {
        "method_id": method_id,
        "title": title,
        "research_stage": "OPEN_DISCOVERY",
        "evidence_label": "EXPLORATORY_ONLY",
        "economic_claim": "FORBIDDEN",
        "uncertainty": {
            "state": "NOT_ESTIMATED_DESCRIPTIVE_OPEN_DISCOVERY",
            "reason": (
                "D3-W2A corrects descriptive estimands only; no confidence "
                "interval, multiplicity-adjusted test, or policy verdict is implied"
            ),
        },
        "status": "NOT_ESTIMABLE",
        "reason": None,
        "summary": [],
        "waterfall": [],
        "queries": [],
        "provenance": {
            "release_ids": list(input_manifest.get("release_ids") or []),
            "exact_objects": _object_provenance(
                input_manifest,
                channels=channels,
                kinds={"dim_snapshot", "capture_gaps_projection"},
            ),
        },
    }


def run_b01(con, input_manifest: dict[str, Any], capabilities: dict[str, Any]) -> dict[str, Any]:
    result = _base_result(
        "D3-B01-MARKOUT",
        "Receive-clock trade-side forward markout surface",
        input_manifest,
        {"orderbooks_l1", "trades"},
    )
    l1_columns = set(capabilities["l1"]["columns"])
    trade_columns = set(capabilities["trades"]["columns"])
    required_l1 = {"local_recv_ts_us", "market_ticker", "yes_bid_e4", "yes_ask_e4"}
    required_trades = {
        "local_recv_ts_us",
        "market_ticker",
        "trade_id",
        "yes_price_e4",
        "taker_side",
    }
    missing = sorted((required_l1 - l1_columns) | (required_trades - trade_columns))
    if missing:
        result["reason"] = "required receive-clock/price fields absent: " + ",".join(missing)
        result["waterfall"] = [{"step": "schema_gate", "count": 0, "reason": result["reason"]}]
        return result

    mid = _mid_logodds_sql("b.yes_bid_e4", "b.yes_ask_e4")
    spread = _spread_logodds_sql("b.yes_bid_e4", "b.yes_ask_e4")
    pre_sql = f"""
      CREATE OR REPLACE TEMP TABLE b01_pre AS
      SELECT t.*,b.t_us AS pre_book_t_us,b.yes_bid_e4,b.yes_ask_e4,
             ({mid}) AS pre_mid_logodds,({spread}) AS spread_logodds,
             10000.0/(1.0+exp(-({mid}))) AS pre_mid_e4,
             CASE WHEN t.occurrence_us IS NULL THEN 'UNKNOWN_PHASE'
                  WHEN t.t_us<t.occurrence_us THEN 'PRE_SCHEDULED_START'
                  ELSE 'POST_SCHEDULED_START_PROXY' END AS phase
      FROM trades_clean t
      ASOF LEFT JOIN (
        SELECT * FROM l1_enriched WHERE book_state='TWO_SIDED'
          AND t_us IS NOT NULL
      ) b
      ON t.date=b.date AND t.market_ticker=b.market_ticker AND t.t_us>b.t_us
    """
    con.execute(pre_sql)
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE b01_pre_fresh AS
        SELECT * FROM b01_pre p
        WHERE pre_book_t_us IS NOT NULL
          AND t_us-pre_book_t_us BETWEEN 0 AND {MAX_BOOK_AGE_US}
          AND NOT EXISTS (
            SELECT 1 FROM capture_gaps g WHERE g.date=p.date
              AND g.start_us<t_us AND g.end_us>pre_book_t_us
          )
        """
    )
    result["queries"].append({"query_id": "B01_PREBOOK", "sql": pre_sql.strip()})
    source = int(scalar(con, "SELECT count(*) FROM trades_norm"))
    clean = int(scalar(con, "SELECT count(*) FROM trades_clean"))
    causal = int(scalar(con, "SELECT count(*) FROM b01_pre WHERE pre_book_t_us IS NOT NULL"))
    fresh = int(scalar(con, "SELECT count(*) FROM b01_pre_fresh"))
    result["waterfall"] = [
        {"step": "source_trade_rows", "count": source, "excluded": 0},
        {"step": "identity_clock_side_price_valid", "count": clean, "excluded": source - clean},
        {"step": "strictly_past_book", "count": causal, "excluded": clean - causal},
        {"step": "book_age_and_gap_clean", "count": fresh, "excluded": causal - fresh},
    ]

    summaries: list[dict[str, Any]] = []
    measurable_max = 0
    for horizon in HORIZONS_US:
        table = f"b01_h{horizon}"
        future_mid = _mid_logodds_sql("f.yes_bid_e4", "f.yes_ask_e4")
        query = f"""
          CREATE OR REPLACE TEMP TABLE {table} AS
          WITH target AS (
            SELECT *,t_us+{horizon} AS target_us FROM b01_pre_fresh
          ), joined AS (
            SELECT p.*,f.t_us AS outcome_book_t_us,
                   ({future_mid}) AS outcome_mid_logodds
            FROM target p
            ASOF LEFT JOIN (
              SELECT * FROM l1_enriched WHERE book_state='TWO_SIDED'
                AND t_us IS NOT NULL
            ) f
            ON p.date=f.date AND p.market_ticker=f.market_ticker
               AND p.target_us>=f.t_us
          )
          SELECT *,taker_sign*(outcome_mid_logodds-pre_mid_logodds)
                     AS signed_markout_logodds
          FROM joined j
          WHERE outcome_book_t_us>t_us AND outcome_book_t_us<=target_us
            AND NOT EXISTS (
              SELECT 1 FROM capture_gaps g WHERE g.date=j.date
                AND g.start_us<outcome_book_t_us AND g.end_us>t_us
            )
        """
        con.execute(query)
        result["queries"].append(
            {"query_id": f"B01_H{horizon}", "sql": query.strip()}
        )
        n = int(scalar(con, f"SELECT count(*) FROM {table}"))
        measurable_max = max(measurable_max, n)
        rows = rows_as_dicts(
            con,
            f"""
            SELECT {horizon} AS horizon_us,coalesce(sport,'_UNKNOWN') AS sport,
                   phase,count(*) AS n,
                   avg(signed_markout_logodds) AS mean_signed_markout_logodds,
                   quantile_cont(signed_markout_logodds,0.5) AS p50_signed_markout_logodds,
                   avg(pre_mid_e4) AS mean_pre_mid_e4,
                   avg(spread_logodds) AS mean_spread_logodds,
                   count(DISTINCT event_proxy) AS root_event_proxies,
                   count(DISTINCT market_ticker) AS markets,
                   count(DISTINCT date) AS days
            FROM {table}
            GROUP BY sport,phase ORDER BY sport,phase
            """,
        )
        for row in rows:
            row["query_id"] = f"B01_H{horizon}"
        summaries.extend(rows)
        result["waterfall"].append(
            {
                "step": f"strict_future_book_within_{horizon}us",
                "count": n,
                "excluded": fresh - n,
            }
        )
    result["summary"] = summaries
    if measurable_max == 0:
        result["reason"] = "no trade had both a fresh past book and a strictly future receive-clock book"
    else:
        result["status"] = "EXECUTED"
        result["reason"] = (
            "descriptive markout surface executed; positive signed values mean "
            "continuation in taker direction and are not NetPnL"
        )
    return result


def run_b02(con, input_manifest: dict[str, Any], capabilities: dict[str, Any]) -> dict[str, Any]:
    result = _base_result(
        "D3-B02-ONESIDE",
        "Censor-correct one-sided state duration and incidence",
        input_manifest,
        {"orderbooks_l1"},
    )
    columns = set(capabilities["l1"]["columns"])
    required = {"local_recv_ts_us", "market_ticker", "yes_bid_e4", "yes_ask_e4"}
    missing = sorted(required - columns)
    if missing:
        result["reason"] = "required L1 state fields absent: " + ",".join(missing)
        result["waterfall"] = [{"step": "schema_gate", "count": 0, "reason": result["reason"]}]
        return result

    query = """
      SELECT coalesce(sport,'_UNKNOWN') AS sport,phase,book_state,
             count(*) AS intervals,count(DISTINCT market_ticker) AS markets,
             count(DISTINCT event_proxy) AS root_event_proxies,
             count(DISTINCT date) AS days,
             sum(duration_us)/1000000.0 AS exposure_seconds,
             quantile_cont(duration_us/1000000.0,0.5) AS p50_duration_seconds,
             quantile_cont(duration_us/1000000.0,0.95) AS p95_duration_seconds
      FROM l1_intervals
      WHERE duration_us>0 AND book_state IN ('TWO_SIDED','ONE_SIDED')
      GROUP BY sport,phase,book_state ORDER BY sport,phase,book_state
    """
    result["queries"].append({"query_id": "B02_STATE_EXPOSURE", "sql": query.strip()})
    rows = rows_as_dicts(con, query)
    for row in rows:
        row["query_id"] = "B02_STATE_EXPOSURE"
    source = int(scalar(con, "SELECT count(*) FROM l1_norm"))
    clocked = int(scalar(con, "SELECT count(*) FROM l1_intervals"))
    right = int(scalar(con, "SELECT count(*) FROM l1_intervals WHERE right_censored"))
    gap = int(scalar(con, "SELECT count(*) FROM l1_intervals WHERE starts_in_gap"))
    valid = int(
        scalar(
            con,
            "SELECT count(*) FROM l1_intervals WHERE duration_us>0 "
            "AND book_state IN ('TWO_SIDED','ONE_SIDED')",
        )
    )
    invalid = int(
        scalar(con, "SELECT count(*) FROM l1_intervals WHERE book_state='INVALID_BOOK'")
    )
    one_us = int(
        scalar(
            con,
            "SELECT coalesce(sum(duration_us),0) FROM l1_intervals "
            "WHERE duration_us>0 AND book_state='ONE_SIDED'",
        )
    )
    total_us = int(
        scalar(
            con,
            "SELECT coalesce(sum(duration_us),0) FROM l1_intervals "
            "WHERE duration_us>0 AND book_state IN ('ONE_SIDED','TWO_SIDED')",
        )
    )
    result["waterfall"] = [
        {"step": "source_l1_rows", "count": source, "excluded": 0},
        {"step": "receive_clock_market_rows", "count": clocked, "excluded": source - clocked},
        {"step": "invalid_book_state", "count": invalid, "excluded": invalid},
        {"step": "right_censored_last_rows", "count": right, "excluded": right},
        {"step": "starts_inside_capture_gap", "count": gap, "excluded": gap},
        {"step": "positive_valid_state_intervals", "count": valid, "excluded": max(0, clocked - valid)},
    ]
    result["summary"] = rows
    result["incidence"] = {
        "one_sided_exposure_us": one_us,
        "valid_state_exposure_us": total_us,
        "one_sided_exposure_share": (one_us / total_us if total_us else None),
        "query_id": "B02_STATE_EXPOSURE",
    }
    if total_us <= 0:
        result["reason"] = "no positive uncensored L1 state exposure after TTL/gap/terminal bounds"
    else:
        result["status"] = "EXECUTED"
        result["reason"] = (
            "state exposure executed with terminal tail right-censored; zero "
            "one-sided exposure is a valid descriptive finding"
        )
    return result


def run_b03(con, input_manifest: dict[str, Any], capabilities: dict[str, Any]) -> dict[str, Any]:
    result = _base_result(
        "D3-B03-XMKT",
        "Quote-age aligned linked-market residual",
        input_manifest,
        {"orderbooks_l1"},
    )
    columns = set(capabilities["l1"]["columns"])
    if {"local_recv_ts_us", "market_ticker"} <= columns:
        query = """
          SELECT count(*) FROM (
            SELECT date,event_proxy,count(DISTINCT market_ticker) AS markets
            FROM l1_enriched
            WHERE t_us IS NOT NULL AND book_state='TWO_SIDED'
              AND event_proxy IS NOT NULL
            GROUP BY date,event_proxy HAVING count(DISTINCT market_ticker)>=2
          )
        """
        candidates = int(scalar(con, query))
        result["queries"].append(
            {"query_id": "B03_LINKED_EVENT_PREFLIGHT", "sql": query.strip()}
        )
    else:
        candidates = 0
    result["estimability_evidence"] = {
        "linked_event_proxy_candidates": candidates,
        "payout_exhaustive_roots": 0,
        "all_leg_fee_facts_complete": 0,
        "strict_multileg_fill_feasible_roots": 0,
        "blocking_reasons": [
            "PAYOUT_EXHAUSTIVENESS_NOT_AUTHENTICATED",
            "LEG_SPECIFIC_FEE_FACTS_NOT_BOUND_TO_INPUT",
            "STRICT_MULTI_LEG_FILL_MODEL_ABSENT",
        ],
    }
    result["waterfall"] = [
        {"step": "linked_event_proxy_candidates", "count": candidates, "excluded": 0},
        {"step": "payout_exhaustiveness_authenticated", "count": 0, "excluded": candidates},
        {"step": "fee_and_fill_feasible", "count": 0, "excluded": candidates},
    ]
    result["reason"] = (
        "NOT_ESTIMABLE: event_ticker proximity does not prove exhaustive payouts; "
        "without leg-specific fees and strict multi-leg fills, a residual would be "
        "a synthetic-arbitrage claim"
    )
    return result


def run_b04(con, input_manifest: dict[str, Any], capabilities: dict[str, Any]) -> dict[str, Any]:
    result = _base_result(
        "D3-B04-RHYTHM",
        "Exposure-adjusted activity rhythm",
        input_manifest,
        {"orderbooks_l1", "trades"},
    )
    l1_columns = set(capabilities["l1"]["columns"])
    trade_columns = set(capabilities["trades"]["columns"])
    required_l1 = {"local_recv_ts_us", "market_ticker", "yes_bid_e4", "yes_ask_e4"}
    required_trade = {"local_recv_ts_us", "market_ticker", "trade_id"}
    missing = sorted((required_l1 - l1_columns) | (required_trade - trade_columns))
    if missing:
        result["reason"] = "active-minute/trade receive-clock fields absent: " + ",".join(missing)
        result["waterfall"] = [{"step": "schema_gate", "count": 0, "reason": result["reason"]}]
        return result

    active_query = """
      CREATE OR REPLACE TEMP TABLE b04_active_minutes AS
      WITH eligible AS (
        SELECT date,market_ticker,event_proxy,coalesce(sport,'_UNKNOWN') AS sport,
               phase,t_us,interval_end_us,
               cast(floor(t_us/60000000) AS BIGINT) AS minute_id
        FROM l1_intervals
        WHERE duration_us>0 AND book_state IN ('ONE_SIDED','TWO_SIDED')
      ), expanded AS (
        SELECT date,market_ticker,event_proxy,sport,phase,minute_id FROM eligible
        UNION ALL
        SELECT date,market_ticker,event_proxy,sport,phase,minute_id+1
        FROM eligible WHERE interval_end_us>(minute_id+1)*60000000
      )
      SELECT DISTINCT date,market_ticker,event_proxy,sport,phase,minute_id
      FROM expanded
    """
    con.execute(active_query)
    trade_query = """
      CREATE OR REPLACE TEMP TABLE b04_trade_minutes AS
      SELECT date,market_ticker,cast(floor(t_us/60000000) AS BIGINT) AS minute_id,
             count(*) AS trades,coalesce(sum(count_e4),0) AS contracts_e4
      FROM trades_dedup GROUP BY date,market_ticker,minute_id
    """
    con.execute(trade_query)
    summary_query = """
      SELECT a.date,a.sport,a.phase,
             cast((a.minute_id % 1440)/60 AS BIGINT) AS utc_hour,
             count(*) AS active_market_minutes,
             count(DISTINCT coalesce(a.event_proxy,a.market_ticker)||':'||a.minute_id)
               AS active_event_minutes,
             count(DISTINCT a.market_ticker) AS markets,
             count(DISTINCT a.event_proxy) AS root_event_proxies,
             coalesce(sum(t.trades),0) AS trades,
             coalesce(sum(t.contracts_e4),0) AS contracts_e4,
             coalesce(sum(t.trades),0)::DOUBLE/count(*) AS trades_per_active_market_minute
      FROM b04_active_minutes a
      LEFT JOIN b04_trade_minutes t USING(date,market_ticker,minute_id)
      GROUP BY a.date,a.sport,a.phase,utc_hour
      ORDER BY a.date,a.sport,a.phase,utc_hour
    """
    result["queries"].extend(
        [
            {"query_id": "B04_ACTIVE_MINUTES", "sql": active_query.strip()},
            {"query_id": "B04_TRADE_MINUTES", "sql": trade_query.strip()},
            {"query_id": "B04_EXPOSURE_ADJUSTED", "sql": summary_query.strip()},
        ]
    )
    rows = rows_as_dicts(con, summary_query)
    for row in rows:
        row["query_id"] = "B04_EXPOSURE_ADJUSTED"
    source_l1 = int(scalar(con, "SELECT count(*) FROM l1_norm"))
    interval_rows = int(scalar(con, "SELECT count(*) FROM l1_intervals WHERE duration_us>0"))
    active = int(scalar(con, "SELECT count(*) FROM b04_active_minutes"))
    source_trades = int(scalar(con, "SELECT count(*) FROM trades_norm"))
    clean_trades = int(scalar(con, "SELECT count(*) FROM trades_dedup"))
    matched_trades = int(
        scalar(
            con,
            "SELECT coalesce(sum(t.trades),0) FROM b04_active_minutes a "
            "JOIN b04_trade_minutes t USING(date,market_ticker,minute_id)",
        )
    )
    result["waterfall"] = [
        {"step": "source_l1_rows", "count": source_l1, "excluded": 0},
        {"step": "positive_gap_clean_state_intervals", "count": interval_rows, "excluded": max(0, source_l1 - interval_rows)},
        {"step": "distinct_active_market_minutes", "count": active, "excluded": 0},
        {"step": "source_trade_rows", "count": source_trades, "excluded": 0},
        {"step": "identity_clock_deduplicated_trades", "count": clean_trades, "excluded": source_trades - clean_trades},
        {"step": "trades_matched_to_active_market_minute", "count": matched_trades, "excluded": max(0, clean_trades - matched_trades)},
    ]
    result["summary"] = rows
    if active <= 0:
        result["reason"] = "no active market-minute exposure after causal interval bounds"
    else:
        result["status"] = "EXECUTED"
        result["reason"] = (
            "UTC rhythm executed with active market-minute/event-minute "
            "denominators; raw clock-hour volume is not used as capacity"
        )
    return result


def execute_all(con, input_manifest: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    capabilities = setup_database(con, input_manifest)
    results = [
        run_b01(con, input_manifest, capabilities),
        run_b02(con, input_manifest, capabilities),
        run_b03(con, input_manifest, capabilities),
        run_b04(con, input_manifest, capabilities),
    ]
    if [result["method_id"] for result in results] != list(METHODS):
        raise RuntimeError("declared/executed method order drift")
    return capabilities, results
