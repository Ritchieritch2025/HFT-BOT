#!/usr/bin/env python3
"""Cycle-1 deep exploratory scan for SPORTS-AUTORESEARCH-01.

The runner is intentionally fail-closed: it consumes only locally VERIFIED
W05 release caches, never opens a network connection, and never emits a
promotion/verdict/live claim.  All strategy-relevant clocks use local receive
time.  Exchange time is retained only for data-quality diagnostics.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path


EXPECTED_INSTANCE = "i-0e53d134dceffe166"
EXPECTED_REGION = "us-east-2"
EXPECTED_ROLE = "w09-research-runner"
EXPECTED_W09_INSTALLATION_SHA256 = {
    "/usr/local/bin/research_data": "68069de774ea00c3547f17a64482dbf90f028d461491eb62892aca32d48b979f",
    "/opt/w09/research/tools/research_data_instance_profile.py": "af1b12e903980827cde6f5b9d08643fdd39855010a862051ffd018fbe6f65caf",
    "/usr/local/bin/w09-run": "6f0fc192f717d1caa77ff85fee02f3dffe3fcf7efcd439b74b0956c567b61321",
    "/etc/w09/cost-contract.json": "bc50854f5b9a60417a015f2d6d9a46284b7d0387be8858ba8fa068f039a8b352",
}
RELEASE_IDS = [
    "2026-07-12__seal-bc37de4c__pub-2bf8871ad4750c03",
    "2026-07-13__seal-7f6e5c1b__pub-f8e4c0abc742b7d5",
]
BANNER = "EXPLORATORY_ONLY · DIAGNOSTIC_ONLY · NOT A LIVE-TRADING AUTHORIZATION"
HORIZONS_US = [
    1_000, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000,
    1_000_000, 2_000_000, 3_000_000, 5_000_000, 10_000_000,
    30_000_000, 120_000_000,
]
BOOK_AGE_CAP_US = 5_000_000


class DeepResearchError(RuntimeError):
    pass


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def path_list(paths: list[Path]) -> str:
    if not paths:
        raise DeepResearchError("empty input path list")
    return "[" + ",".join(quote(str(path)) for path in paths) + "]"


def source_expr(
    available: set[str], candidates: tuple[str, ...], alias: str, sql_type: str,
) -> str:
    """Build a fail-explicit optional CSV projection.

    Historical dimension snapshots have genuine column drift.  Reading every
    field as VARCHAR avoids inference-dependent coercion, and missing optional
    columns become typed NULLs rather than binder errors.  Required identity
    fields are checked after materialization.
    """
    present = [f'"{name}"' for name in candidates if name in available]
    if not present:
        base = "NULL"
    elif len(present) == 1:
        base = present[0]
    else:
        base = "coalesce(" + ",".join(present) + ")"
    return f'try_cast({base} AS {sql_type}) AS "{alias}"'


def scalar(con, sql: str):
    return con.execute(sql).fetchone()[0]


def rows_as_dicts(con, sql: str) -> list[dict]:
    cur = con.execute(sql)
    names = [item[0] for item in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def mid_logodds_sql(bid: str, ask: str) -> str:
    def lo(column: str) -> str:
        clipped = f"greatest(1.0,least(9999.0,{column}))"
        return f"ln(({clipped})/(10000.0-({clipped})))"
    return f"(({lo(bid)})+({lo(ask)}))/2.0"


def expit_e4_sql(logodds: str) -> str:
    return f"10000.0/(1.0+exp(-({logodds})))"


def manifest_inventory_class(key: str) -> str:
    """Classify every immutable manifest object, including non-analytic evidence."""
    if key.startswith("facts/orderbooks_l1/"):
        return "facts/orderbooks_l1"
    if key.startswith("facts/orderbooks_full/"):
        return "facts/orderbooks_full_l2"
    if key.startswith("facts/trades/"):
        return "facts/trades"
    if key.startswith("dim/snapshots/"):
        name = Path(key).name
        return f"dim/snapshots/{name}"
    if key.startswith("raw_rfq/"):
        return "raw_rfq/receipts" if "rfq_receipts_" in Path(key).name else "raw_rfq/messages"
    if key.startswith("quality/"):
        return f"quality/{Path(key).name}"
    if key.startswith("corrections/"):
        return "corrections"
    return key.split("/", 1)[0]


def discover_release_inputs(cache_root: Path) -> dict:
    releases = []
    for release_id in RELEASE_IDS:
        base = cache_root / "releases" / release_id
        manifest = base / "MANIFEST.json"
        verified = base / ".VERIFIED.json"
        if not manifest.is_file() or not verified.is_file():
            raise DeepResearchError(f"release is not locally VERIFIED: {release_id}")
        marker = json.loads(verified.read_text(encoding="utf-8"))
        manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
        if marker.get("release_id") != release_id:
            raise DeepResearchError(f"VERIFIED marker identity mismatch: {release_id}")
        if marker.get("version_binding_mode") != "VERSION_BOUND":
            raise DeepResearchError(f"release is not VERSION_BOUND: {release_id}")
        if marker.get("evidence_tier") != "SEALED_DEGRADED_EVIDENCE":
            raise DeepResearchError(f"unexpected evidence tier: {release_id}")
        if manifest_value.get("release_id") != release_id:
            raise DeepResearchError(f"MANIFEST identity mismatch: {release_id}")
        releases.append({"release_id": release_id, "base": base, "marker": marker,
                         "manifest": manifest_value})
    inputs = {
        "releases": releases,
        "l1": [], "trades": [], "l2": [],
        "dim_markets": [], "dim_events": [], "dim_series": [], "rfq": [],
        "gaps": [], "l2_gap_receipts": [], "object_bindings": [],
        "manifest_inventory": [],
        "rfq_duplicate_objects_skipped": [],
    }
    rfq_seen: dict[str, dict] = {}
    for release in releases:
        base = release["base"]
        for obj in sorted(release["manifest"]["objects"], key=lambda item: item["key"]):
            key = obj["key"]
            target = base / key
            inputs["manifest_inventory"].append({
                "release_id": release["release_id"], "key": key,
                "version_id": obj.get("version_id"), "sha256": obj.get("sha256"),
                "bytes": obj.get("size"),
                "inventory_class": manifest_inventory_class(key),
            })
            channel = None
            if key.startswith("facts/orderbooks_l1/category=Sports/") and key.endswith(".parquet"):
                channel = "l1"
            elif key.startswith("facts/trades/category=Sports/") and (
                    key.endswith(".csv") or key.endswith(".csv.gz")):
                channel = "trades"
            elif key.startswith("facts/orderbooks_full/category=Sports/") and key.endswith(".parquet"):
                channel = "l2"
            elif key.startswith("dim/snapshots/date=") and key.endswith("/markets.csv"):
                channel = "dim_markets"
            elif key.startswith("dim/snapshots/date=") and key.endswith("/events.csv"):
                channel = "dim_events"
            elif key.startswith("dim/snapshots/date=") and key.endswith("/series.csv"):
                channel = "dim_series"
            elif key.startswith("raw_rfq/") and ".ndjson" in Path(key).name:
                channel = "rfq"
            elif key.startswith("quality/capture_gaps_") and key.endswith(".csv"):
                channel = "gaps"
            elif key == "quality/l2_gaps.json":
                channel = "l2_gap_receipts"
            if channel is None:
                continue
            if not target.is_file():
                raise DeepResearchError(f"manifest-bound object missing locally: {release['release_id']}:{key}")
            binding = {
                "release_id": release["release_id"], "key": key,
                "version_id": obj.get("version_id"), "sha256": obj.get("sha256"),
                "bytes": obj.get("size"), "channel": channel,
            }
            if channel == "rfq" and key in rfq_seen:
                prior = rfq_seen[key]
                if prior.get("sha256") != obj.get("sha256"):
                    raise DeepResearchError(f"overlapping RFQ object has different bytes: {key}")
                inputs["rfq_duplicate_objects_skipped"].append({
                    "key": key, "kept_release_id": prior["release_id"],
                    "skipped_release_id": release["release_id"],
                    "sha256": obj.get("sha256"),
                })
                continue
            if channel == "rfq":
                rfq_seen[key] = binding
            inputs[channel].append(target)
            inputs["object_bindings"].append(binding)
    for key in ("l1", "trades", "l2", "dim_markets", "dim_events", "dim_series",
                "rfq", "gaps", "l2_gap_receipts"):
        if not inputs[key]:
            raise DeepResearchError(f"required input class is empty: {key}")
    return inputs


def configure(
    con, run_dir: Path, memory_limit: str, threads: int, max_temp_directory_size: str,
) -> None:
    temp = run_dir / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET memory_limit={quote(memory_limit)}")
    con.execute(f"SET threads={int(threads)}")
    con.execute(f"SET temp_directory={quote(str(temp))}")
    con.execute(f"SET max_temp_directory_size={quote(max_temp_directory_size)}")
    con.execute("SET preserve_insertion_order=false")
    con.execute("SET TimeZone='UTC'")


def make_views(con, inputs: dict) -> None:
    l1 = path_list(inputs["l1"])
    trades = path_list(inputs["trades"])
    l2 = path_list(inputs["l2"])
    gaps = path_list(inputs["gaps"])
    con.execute(f"""
      CREATE OR REPLACE VIEW l1_all AS
      SELECT date, local_recv_ts_us AS t_us, exchange_ts_us,
             recv_wall_ns, recv_mono_ns, market_ticker, event_ticker,
             series_ticker, subcategory AS sport, "group" AS league,
             yes_bid_e4, yes_bid_qty_e4, yes_ask_e4, yes_ask_qty_e4,
             is_snapshot
      FROM read_parquet({l1}, union_by_name=true, hive_partitioning=true)
      WHERE local_recv_ts_us IS NOT NULL
    """)
    con.execute(f"""
      CREATE OR REPLACE TABLE l1_ingest_by_market AS
      SELECT date,market_ticker,subcategory AS sport,"group" AS league,
             count(*) AS source_rows,
             count(*) FILTER (WHERE local_recv_ts_us IS NULL) AS missing_local_receive_rows
      FROM read_parquet({l1},union_by_name=true,hive_partitioning=true)
      GROUP BY date,market_ticker,subcategory,"group"
    """)
    con.execute("""
      CREATE OR REPLACE TABLE l1_ingest_qc AS
      SELECT date,sum(source_rows) AS source_rows,
             sum(missing_local_receive_rows) AS missing_local_receive_rows,
             count(*) AS source_markets,
             count(*) FILTER (WHERE source_rows>missing_local_receive_rows)
               AS receive_clock_markets
      FROM l1_ingest_by_market GROUP BY date ORDER BY date
    """)
    # Rows with NULL local receive time were removed above.  Remaining initial
    # snapshots are genuinely received state and must remain eligible for ASOF;
    # they do not independently trigger any study in this runner.
    con.execute("""
      CREATE OR REPLACE VIEW l1_real AS
      SELECT * FROM l1_all
    """)
    con.execute(f"""
      CREATE OR REPLACE TABLE trades_raw AS
      SELECT date, local_recv_ts_us AS t_us, exchange_ts_us,
             recv_wall_ns, recv_mono_ns, market_ticker, event_ticker,
             series_ticker, subcategory AS sport, "group" AS league,
             trade_id, yes_price_e4, no_price_e4, count_e4,
             CASE WHEN lower(taker_side)='yes' THEN 1
                  WHEN lower(taker_side)='no' THEN -1 ELSE NULL END AS taker_sign
      FROM read_csv({trades}, header=true, union_by_name=true,
                    hive_partitioning=true, auto_detect=true,
                    types={{'trade_id':'VARCHAR','market_ticker':'VARCHAR',
                            'event_ticker':'VARCHAR','series_ticker':'VARCHAR',
                            'taker_side':'VARCHAR','yes_price_e4':'BIGINT',
                            'no_price_e4':'BIGINT','count_e4':'BIGINT',
                            'local_recv_ts_us':'BIGINT'}})
    """)
    con.execute("""
      CREATE OR REPLACE TABLE trade_id_qc AS
      WITH signed AS (
        SELECT *,hash(struct_pack(
          date := date,t_us := t_us,exchange_ts_us := exchange_ts_us,
          recv_wall_ns := recv_wall_ns,recv_mono_ns := recv_mono_ns,
          market_ticker := market_ticker,event_ticker := event_ticker,
          series_ticker := series_ticker,sport := sport,league := league,
          yes_price_e4 := yes_price_e4,no_price_e4 := no_price_e4,
          count_e4 := count_e4,taker_sign := taker_sign
        )) AS economic_signature
        FROM trades_raw WHERE trade_id IS NOT NULL
      )
      SELECT trade_id,count(*) AS raw_rows,
             count(DISTINCT economic_signature) AS economic_variants
      FROM signed GROUP BY trade_id
    """)
    con.execute("""
      CREATE OR REPLACE TABLE trades_safe AS
      SELECT * EXCLUDE(rn)
      FROM (
        SELECT t.*, row_number() OVER (
          PARTITION BY t.trade_id
          ORDER BY t.t_us,t.recv_wall_ns,t.recv_mono_ns,t.market_ticker,
                   t.yes_price_e4,t.no_price_e4,t.count_e4
        ) AS rn
        FROM trades_raw t JOIN trade_id_qc c USING(trade_id)
        WHERE t.t_us IS NOT NULL AND c.economic_variants=1 AND t.taker_sign IS NOT NULL
          AND t.count_e4>0 AND t.yes_price_e4 BETWEEN 1 AND 9999
          AND t.no_price_e4 BETWEEN 1 AND 9999
          AND t.yes_price_e4+t.no_price_e4=10000
      ) WHERE rn=1
    """)
    con.execute(f"""
      CREATE OR REPLACE VIEW l2_all AS
      SELECT date, local_recv_ts_us AS t_us, exchange_ts_us,
             recv_wall_ns, recv_mono_ns, market_ticker, event_ticker,
             series_ticker, subcategory AS sport, "group" AS league,
             msg_type, side, price_e4, delta_e4, yes_levels, no_levels,
             ws_sid, ws_seq
      FROM read_parquet({l2}, union_by_name=true, hive_partitioning=true)
      WHERE local_recv_ts_us IS NOT NULL
    """)
    con.execute(f"""
      CREATE OR REPLACE TABLE l2_ingest_by_market AS
      SELECT date,market_ticker,subcategory AS sport,"group" AS league,
             count(*) AS source_rows,
             count(*) FILTER (WHERE local_recv_ts_us IS NULL) AS missing_local_receive_rows
      FROM read_parquet({l2},union_by_name=true,hive_partitioning=true)
      GROUP BY date,market_ticker,subcategory,"group"
    """)
    con.execute("""
      CREATE OR REPLACE TABLE l2_ingest_qc AS
      SELECT date,sum(source_rows) AS source_rows,
             sum(missing_local_receive_rows) AS missing_local_receive_rows,
             count(*) AS source_markets,
             count(*) FILTER (WHERE source_rows>missing_local_receive_rows)
               AS receive_clock_markets
      FROM l2_ingest_by_market GROUP BY date ORDER BY date
    """)
    con.execute("""
      CREATE OR REPLACE TABLE l1_day_bounds AS
      SELECT date,min(t_us) AS first_t_us,max(t_us) AS last_t_us,
             count(*) AS rows,count(DISTINCT market_ticker) AS n_markets
      FROM l1_all GROUP BY date
    """)
    con.execute("""
      CREATE OR REPLACE TABLE l1_global_bounds AS
      SELECT min(t_us) AS first_t_us,max(t_us) AS last_t_us,count(*) AS rows,
             count(DISTINCT market_ticker) AS n_markets
      FROM l1_all
    """)
    con.execute(f"""
      CREATE OR REPLACE TABLE capture_gaps AS
      SELECT cast(start_us AS BIGINT) AS start_us,cast(end_us AS BIGINT) AS end_us
      FROM read_csv({gaps}, header=true, union_by_name=true, auto_detect=true)
      WHERE start_us IS NOT NULL AND end_us IS NOT NULL AND end_us>start_us
    """)


def materialize_dimensions(con, inputs: dict) -> None:
    markets = path_list(inputs["dim_markets"])
    events = path_list(inputs["dim_events"])
    series = path_list(inputs["dim_series"])
    con.execute("""
      CREATE OR REPLACE TABLE fact_market_identity_qc AS
      SELECT date,market_ticker,count(*) AS fact_rows,
             count(DISTINCT hash(struct_pack(
               event_ticker := event_ticker,series_ticker := series_ticker,
               sport := sport,league := league))) AS identity_variants
      FROM l1_all GROUP BY date,market_ticker
    """)
    con.execute("""
      CREATE OR REPLACE TABLE sports_market_ids AS
      SELECT l.date,l.market_ticker,min(l.event_ticker) AS fact_event_ticker,
             min(l.series_ticker) AS series_ticker,min(l.sport) AS sport,
             min(l.league) AS league
      FROM l1_all l JOIN fact_market_identity_qc q USING(date,market_ticker)
      WHERE q.identity_variants=1 GROUP BY l.date,l.market_ticker
    """)

    # Dimension snapshots are large CSVs with real cross-day schema/type drift.
    # Parse them strictly as VARCHAR, project an explicit typed schema, and make
    # every optional absence visible as typed NULL.  Required identity columns
    # fail the run before any inferential table is built.
    con.execute(f"""
      CREATE OR REPLACE VIEW dim_markets_source AS
      SELECT * FROM read_csv({markets},header=true,union_by_name=true,
                             hive_partitioning=true,all_varchar=true)
    """)
    market_columns = {item[0] for item in con.execute(
        "SELECT * FROM dim_markets_source LIMIT 0").description}
    missing = {"date", "ticker", "event_ticker"} - market_columns
    if missing:
        raise DeepResearchError(f"required market dimension columns missing: {sorted(missing)}")
    market_projection = [
        source_expr(market_columns, ("date",), "date", "DATE"),
        source_expr(market_columns, ("ticker",), "market_ticker", "VARCHAR"),
        source_expr(market_columns, ("event_ticker",), "canonical_event_ticker", "VARCHAR"),
        source_expr(market_columns, ("occurrence_datetime",), "occurrence_datetime_source", "VARCHAR"),
        source_expr(market_columns, ("occurrence_datetime",), "occurrence_datetime", "TIMESTAMPTZ"),
        source_expr(market_columns, ("updated_time",), "market_updated_time_source", "VARCHAR"),
        source_expr(market_columns, ("updated_time",), "market_updated_time", "TIMESTAMPTZ"),
        source_expr(market_columns, ("expected_expiration_time", "expected_expiration"), "expected_expiration_time", "TIMESTAMPTZ"),
        source_expr(market_columns, ("close_time", "close"), "close_time", "TIMESTAMPTZ"),
        source_expr(market_columns, ("event_structure",), "event_structure", "VARCHAR"),
        source_expr(market_columns, ("bracket_rank",), "bracket_rank", "VARCHAR"),
        source_expr(market_columns, ("strike_type",), "strike_type", "VARCHAR"),
        source_expr(market_columns, ("custom_strike",), "custom_strike", "VARCHAR"),
        source_expr(market_columns, ("primary_participant_key",), "primary_participant_key", "VARCHAR"),
        source_expr(market_columns, ("yes_sub_title",), "yes_sub_title", "VARCHAR"),
        source_expr(market_columns, ("no_sub_title",), "no_sub_title", "VARCHAR"),
        source_expr(market_columns, ("title",), "market_title", "VARCHAR"),
        source_expr(market_columns, ("rules_primary",), "rules_primary", "VARCHAR"),
        source_expr(market_columns, ("rules_secondary",), "rules_secondary", "VARCHAR"),
        source_expr(market_columns, ("market_type",), "market_type", "VARCHAR"),
        source_expr(market_columns, ("mve_collection_ticker",), "mve_collection_ticker", "VARCHAR"),
        source_expr(market_columns, ("mve_selected_legs",), "mve_selected_legs", "VARCHAR"),
        source_expr(market_columns, ("floor_strike",), "floor_strike", "VARCHAR"),
        source_expr(market_columns, ("cap_strike",), "cap_strike", "VARCHAR"),
    ]
    con.execute("CREATE OR REPLACE VIEW dim_markets_raw AS SELECT " +
                ",".join(market_projection) + " FROM dim_markets_source")
    con.execute("""
      CREATE OR REPLACE TABLE dim_market_candidates AS
      SELECT d.* FROM dim_markets_raw d
      JOIN sports_market_ids s USING(date,market_ticker)
    """)
    con.execute("""
      CREATE OR REPLACE TABLE dim_market_key_qc AS
      SELECT d.date,d.market_ticker,count(*) AS raw_rows,
             count(DISTINCT hash(struct_pack(
               canonical_event_ticker := d.canonical_event_ticker,
               occurrence_datetime := d.occurrence_datetime,
               market_updated_time := d.market_updated_time,
               event_structure := d.event_structure,yes_sub_title := d.yes_sub_title,
               no_sub_title := d.no_sub_title,market_title := d.market_title,
               market_type := d.market_type,mve_collection_ticker := d.mve_collection_ticker,
               mve_selected_legs := d.mve_selected_legs))) AS critical_variants
      FROM dim_market_candidates d GROUP BY d.date,d.market_ticker
    """)
    con.execute("""
      CREATE OR REPLACE TABLE dim_markets AS
      SELECT d.date,d.market_ticker,min(d.canonical_event_ticker) AS canonical_event_ticker,
             min(d.occurrence_datetime) AS occurrence_datetime,
             min(d.market_updated_time) AS market_updated_time,
             min(d.expected_expiration_time) AS expected_expiration_time,
             min(d.close_time) AS close_time,min(d.event_structure) AS event_structure,
             min(d.bracket_rank) AS bracket_rank,min(d.strike_type) AS strike_type,
             min(d.custom_strike) AS custom_strike,
             min(d.primary_participant_key) AS primary_participant_key,
             min(d.yes_sub_title) AS yes_sub_title,min(d.no_sub_title) AS no_sub_title,
             min(d.market_title) AS market_title,min(d.rules_primary) AS rules_primary,
             min(d.rules_secondary) AS rules_secondary,min(d.market_type) AS market_type,
             min(d.mve_collection_ticker) AS mve_collection_ticker,
             min(d.mve_selected_legs) AS mve_selected_legs,
             min(d.floor_strike) AS floor_strike,min(d.cap_strike) AS cap_strike
      FROM dim_market_candidates d JOIN dim_market_key_qc q USING(date,market_ticker)
      WHERE q.critical_variants=1 GROUP BY d.date,d.market_ticker
    """)

    con.execute(f"""
      CREATE OR REPLACE VIEW dim_events_source AS
      SELECT * FROM read_csv({events},header=true,union_by_name=true,
                             hive_partitioning=true,all_varchar=true)
    """)
    event_columns = {item[0] for item in con.execute(
        "SELECT * FROM dim_events_source LIMIT 0").description}
    missing = {"date", "event_ticker"} - event_columns
    if missing:
        raise DeepResearchError(f"required event dimension columns missing: {sorted(missing)}")
    event_projection = [
        source_expr(event_columns, ("date",), "date", "DATE"),
        source_expr(event_columns, ("event_ticker",), "event_ticker", "VARCHAR"),
        source_expr(event_columns, ("series_ticker",), "series_ticker", "VARCHAR"),
        source_expr(event_columns, ("mutually_exclusive",), "mutually_exclusive", "BOOLEAN"),
        source_expr(event_columns, ("last_updated_ts",), "event_updated_time_source", "VARCHAR"),
        source_expr(event_columns, ("last_updated_ts",), "event_updated_time", "TIMESTAMPTZ"),
        source_expr(event_columns, ("title",), "event_title", "VARCHAR"),
        source_expr(event_columns, ("sub_title",), "event_sub_title", "VARCHAR"),
        source_expr(event_columns, ("category",), "category", "VARCHAR"),
    ]
    con.execute("CREATE OR REPLACE VIEW dim_events_raw AS SELECT " +
                ",".join(event_projection) + " FROM dim_events_source")
    con.execute("""
      CREATE OR REPLACE TABLE dim_event_candidates AS
      SELECT e.* FROM dim_events_raw e
      WHERE EXISTS (
        SELECT 1 FROM sports_market_ids s
        WHERE s.date=e.date AND s.fact_event_ticker=e.event_ticker
      ) OR EXISTS (
        SELECT 1 FROM dim_markets m
        WHERE m.date=e.date AND m.canonical_event_ticker=e.event_ticker
      )
    """)
    con.execute("""
      CREATE OR REPLACE TABLE dim_event_key_qc AS
      SELECT date,event_ticker,count(*) AS raw_rows,
             count(DISTINCT hash(struct_pack(series_ticker := series_ticker,
               mutually_exclusive := mutually_exclusive,event_title := event_title,
               event_updated_time := event_updated_time,
               event_sub_title := event_sub_title,category := category))) AS critical_variants
      FROM dim_event_candidates GROUP BY date,event_ticker
    """)
    con.execute("""
      CREATE OR REPLACE TABLE dim_events AS
      SELECT e.date,e.event_ticker,min(e.series_ticker) AS series_ticker,
             min(e.mutually_exclusive) AS mutually_exclusive,
             min(e.event_updated_time) AS event_updated_time,
             min(e.event_title) AS event_title,min(e.event_sub_title) AS event_sub_title,
             min(e.category) AS category
      FROM dim_event_candidates e JOIN dim_event_key_qc q USING(date,event_ticker)
      WHERE q.critical_variants=1 GROUP BY e.date,e.event_ticker
    """)

    con.execute(f"""
      CREATE OR REPLACE VIEW dim_series_source AS
      SELECT * FROM read_csv({series},header=true,union_by_name=true,
                             hive_partitioning=true,all_varchar=true)
    """)
    series_columns = {item[0] for item in con.execute(
        "SELECT * FROM dim_series_source LIMIT 0").description}
    missing = {"date", "ticker"} - series_columns
    if missing:
        raise DeepResearchError(f"required series dimension columns missing: {sorted(missing)}")
    series_projection = [
        source_expr(series_columns, ("date",), "date", "DATE"),
        source_expr(series_columns, ("ticker",), "series_ticker", "VARCHAR"),
        source_expr(series_columns, ("title",), "series_title", "VARCHAR"),
        source_expr(series_columns, ("fee_type",), "fee_type", "VARCHAR"),
        source_expr(series_columns, ("fee_multiplier",), "fee_multiplier", "VARCHAR"),
        source_expr(series_columns, ("category",), "category", "VARCHAR"),
    ]
    con.execute("CREATE OR REPLACE VIEW dim_series_raw AS SELECT " +
                ",".join(series_projection) + " FROM dim_series_source")
    con.execute("""
      CREATE OR REPLACE TABLE dim_series_candidates AS
      SELECT d.* FROM dim_series_raw d
      WHERE EXISTS (SELECT 1 FROM sports_market_ids s
                    WHERE s.date=d.date AND s.series_ticker=d.series_ticker)
    """)
    con.execute("""
      CREATE OR REPLACE TABLE dim_series_key_qc AS
      SELECT date,series_ticker,count(*) AS raw_rows,
             count(DISTINCT hash(struct_pack(series_title := series_title,
               fee_type := fee_type,fee_multiplier := fee_multiplier,
               category := category))) AS critical_variants
      FROM dim_series_candidates GROUP BY date,series_ticker
    """)
    con.execute("""
      CREATE OR REPLACE TABLE dim_series AS
      SELECT d.date,d.series_ticker,min(d.series_title) AS series_title,
             min(d.fee_type) AS fee_type,min(d.fee_multiplier) AS fee_multiplier,
             min(d.category) AS category
      FROM dim_series_candidates d JOIN dim_series_key_qc q USING(date,series_ticker)
      WHERE q.critical_variants=1 GROUP BY d.date,d.series_ticker
    """)

    con.execute("""
      CREATE OR REPLACE TABLE dim_cast_qc AS
      SELECT 'markets.occurrence_datetime' AS field,
             count(*) FILTER (WHERE nullif(trim(occurrence_datetime_source),'') IS NOT NULL)
               AS source_nonnull,
             count(*) FILTER (WHERE nullif(trim(occurrence_datetime_source),'') IS NOT NULL
                               AND occurrence_datetime IS NULL) AS cast_failures
      FROM dim_market_candidates
      UNION ALL
      SELECT 'markets.updated_time',
             count(*) FILTER (WHERE nullif(trim(market_updated_time_source),'') IS NOT NULL),
             count(*) FILTER (WHERE nullif(trim(market_updated_time_source),'') IS NOT NULL
                               AND market_updated_time IS NULL)
      FROM dim_market_candidates
      UNION ALL
      SELECT 'events.last_updated_ts',
             count(*) FILTER (WHERE nullif(trim(event_updated_time_source),'') IS NOT NULL),
             count(*) FILTER (WHERE nullif(trim(event_updated_time_source),'') IS NOT NULL
                               AND event_updated_time IS NULL)
      FROM dim_event_candidates
    """)

    # The title/time mapping is deliberately provisional.  It may join sibling
    # families, but a canonical family pointing to multiple heuristic roots is
    # ambiguous and is excluded rather than silently split.
    con.execute("""
      CREATE OR REPLACE TABLE universe_candidates AS
      WITH joined AS (
        SELECT s.*,m.canonical_event_ticker,m.occurrence_datetime,m.market_updated_time,
               m.expected_expiration_time,m.close_time,m.event_structure,
               m.bracket_rank,m.strike_type,m.custom_strike,
               m.primary_participant_key,m.yes_sub_title,m.no_sub_title,
               m.market_title,m.rules_primary,m.rules_secondary,m.market_type,
               m.mve_collection_ticker,m.mve_selected_legs,m.floor_strike,m.cap_strike,
               e.event_title,e.event_sub_title,e.mutually_exclusive,e.event_updated_time,
               ds.series_title,ds.fee_type,ds.fee_multiplier,
               trim(regexp_replace(lower(split_part(coalesce(e.event_title,m.market_title,''),':',1)),
                                   '[^a-z0-9]+',' ','g')) AS matchup_key
        FROM sports_market_ids s
        LEFT JOIN dim_markets m USING(date,market_ticker)
        LEFT JOIN dim_events e
          ON e.date=s.date AND e.event_ticker=m.canonical_event_ticker
        LEFT JOIN dim_series ds
          ON ds.date=s.date AND ds.series_ticker=s.series_ticker
      ), candidates AS (
        SELECT *,CASE
                   WHEN market_updated_time IS NULL THEN NULL
                   WHEN event_title IS NOT NULL AND event_updated_time IS NULL THEN NULL
                   ELSE greatest(epoch_us(market_updated_time),
                                 coalesce(epoch_us(event_updated_time),epoch_us(market_updated_time)))
                 END AS dim_effective_us,
                 CASE WHEN occurrence_datetime IS NOT NULL
                           AND regexp_matches(' '||matchup_key||' ',' (vs|at|v) ')
                      THEN md5(matchup_key||'|'||cast(occurrence_datetime AS VARCHAR))
                      ELSE NULL END AS candidate_root_event_id
        FROM joined
      )
      SELECT * FROM candidates
    """)
    con.execute("""
      CREATE OR REPLACE TABLE provisional_root_qc AS
      SELECT candidate_root_event_id,count(*) AS market_days,
             count(DISTINCT matchup_key) AS distinct_matchup_keys,
             count(DISTINCT cast(occurrence_datetime AS VARCHAR)) AS distinct_occurrences,
             count(DISTINCT lower(sport)) FILTER (
               WHERE sport IS NOT NULL AND lower(sport) NOT IN ('none','unknown',''))
               AS distinct_nonempty_sports,
             min(sport) FILTER (
               WHERE sport IS NOT NULL AND lower(sport) NOT IN ('none','unknown',''))
               AS normalized_sport
      FROM universe_candidates WHERE candidate_root_event_id IS NOT NULL
      GROUP BY candidate_root_event_id
    """)
    con.execute("""
      CREATE OR REPLACE TABLE family_root_qc AS
      SELECT date,canonical_event_ticker AS family_event_id,
             count(DISTINCT candidate_root_event_id) AS candidate_roots,
             string_agg(DISTINCT candidate_root_event_id,',' ORDER BY candidate_root_event_id)
               AS candidate_root_ids
      FROM universe_candidates
      WHERE canonical_event_ticker IS NOT NULL AND candidate_root_event_id IS NOT NULL
      GROUP BY date,canonical_event_ticker
    """)
    con.execute("""
      CREATE OR REPLACE TABLE universe AS
      SELECT c.* EXCLUDE(sport,candidate_root_event_id),c.sport AS fact_sport,
        coalesce(q.normalized_sport,c.sport) AS sport,
        CASE WHEN c.candidate_root_event_id IS NULL THEN 'UNMAPPED'
             WHEN q.distinct_matchup_keys<>1 OR q.distinct_occurrences<>1
               THEN 'AMBIGUOUS_ROOT_COLLISION'
             WHEN q.distinct_nonempty_sports>1 THEN 'AMBIGUOUS_SPORT_CONFLICT'
             WHEN coalesce(f.candidate_roots,1)>1 THEN 'AMBIGUOUS_FAMILY_MULTIROOT'
             ELSE 'PROVISIONAL_HEURISTIC_MATCHUP_TIME' END AS root_map_status,
        CASE WHEN c.candidate_root_event_id IS NOT NULL
                   AND q.distinct_matchup_keys=1 AND q.distinct_occurrences=1
                   AND q.distinct_nonempty_sports<=1
                   AND coalesce(f.candidate_roots,1)=1
             THEN c.candidate_root_event_id ELSE NULL END AS root_event_id,
        c.canonical_event_ticker AS family_event_id
      FROM universe_candidates c
      LEFT JOIN provisional_root_qc q USING(candidate_root_event_id)
      LEFT JOIN family_root_qc f
        ON f.date=c.date AND f.family_event_id=c.canonical_event_ticker
    """)


def build_atlas(con, run_dir: Path) -> dict:
    con.execute("""
      CREATE OR REPLACE TABLE l1_intervals AS
      WITH ordered AS (
        SELECT l.date,l.t_us,l.recv_wall_ns,l.recv_mono_ns,l.market_ticker,
               l.event_ticker,l.series_ticker,l.sport,l.league,
               l.yes_bid_e4,l.yes_ask_e4,b.last_t_us AS day_last_t_us,
               lead(t_us) OVER (
                 PARTITION BY date,market_ticker
                 ORDER BY t_us,recv_wall_ns,recv_mono_ns
               ) AS next_t_us
        FROM l1_real l JOIN l1_day_bounds b USING(date)
      ), bounded AS (
        SELECT *,least(coalesce(next_t_us,day_last_t_us),t_us+60000000,day_last_t_us)
                    AS interval_end_us
        FROM ordered
      )
      SELECT o.date,o.t_us,o.market_ticker,o.event_ticker,o.series_ticker,
             o.sport,o.league,o.yes_bid_e4,o.yes_ask_e4,o.interval_end_us,
             CASE WHEN EXISTS (
                    SELECT 1 FROM capture_gaps g
                    WHERE g.start_us<o.interval_end_us
                      AND g.end_us>o.t_us
                  ) THEN 0
                  ELSE greatest(0,o.interval_end_us-o.t_us)
             END AS dt_us,
             yes_bid_e4 IS NOT NULL AND yes_ask_e4 IS NOT NULL
               AND yes_bid_e4>0 AND yes_ask_e4<10000 AND yes_bid_e4<yes_ask_e4
               AS valid_two_sided
      FROM bounded o
    """)
    spread_lo = mid_logodds_sql("yes_bid_e4", "yes_ask_e4").replace("/2.0", "")
    # Replace midpoint sum with ask-minus-bid for the spread.
    bid_lo = "ln(greatest(1.0,least(9999.0,yes_bid_e4))/(10000.0-greatest(1.0,least(9999.0,yes_bid_e4))))"
    ask_lo = "ln(greatest(1.0,least(9999.0,yes_ask_e4))/(10000.0-greatest(1.0,least(9999.0,yes_ask_e4))))"
    spread_lo = f"({ask_lo})-({bid_lo})"
    con.execute(f"""
      CREATE OR REPLACE TABLE atlas_market AS
      WITH trade_counts AS (
        SELECT date,market_ticker,count(*) AS trades,
               sum(count_e4)/10000.0 AS contracts
        FROM trades_safe GROUP BY 1,2
      )
      SELECT i.date,i.market_ticker,min(i.event_ticker) AS fact_event_ticker,
             min(i.series_ticker) AS series_ticker,
             min(i.sport) AS sport,min(i.league) AS league,
             count(*) AS real_l1_rows,min(i.t_us) AS first_t_us,max(i.t_us) AS last_t_us,
             sum(i.dt_us)/1000000.0 AS trusted_nonstale_seconds,
             sum(CASE WHEN i.valid_two_sided THEN i.dt_us ELSE 0 END)/1000000.0
               AS two_sided_seconds,
             sum(CASE WHEN i.valid_two_sided THEN i.dt_us ELSE 0 END)/
               nullif(sum(i.dt_us),0) AS two_sided_share,
             sum(CASE WHEN i.valid_two_sided THEN ({spread_lo})*i.dt_us ELSE 0 END)/
               nullif(sum(CASE WHEN i.valid_two_sided THEN i.dt_us ELSE 0 END),0)
               AS weighted_spread_logodds,
             sum(CASE WHEN i.valid_two_sided THEN (i.yes_ask_e4-i.yes_bid_e4)*cast(i.dt_us AS DOUBLE) ELSE 0 END)/
               nullif(sum(CASE WHEN i.valid_two_sided THEN i.dt_us ELSE 0 END),0)
               AS weighted_spread_e4,
             coalesce(max(t.trades),0) AS trades,
             coalesce(max(t.contracts),0) AS contracts
      FROM l1_intervals i LEFT JOIN trade_counts t USING(date,market_ticker)
      GROUP BY i.date,i.market_ticker
    """)
    con.execute("""
      CREATE OR REPLACE TABLE atlas_root AS
      SELECT min(u.date) AS first_date,max(u.date) AS last_date,u.root_event_id,
             min(u.matchup_key) AS matchup_key,min(u.occurrence_datetime) AS occurrence_datetime,
             min(u.sport) AS sport,string_agg(DISTINCT u.league,',' ORDER BY u.league) AS leagues,
             count(DISTINCT u.date) AS n_day_blocks,count(*) AS n_market_days,
             count(DISTINCT u.market_ticker) AS n_markets,
             count(DISTINCT u.family_event_id) AS n_family_events,
             sum(a.real_l1_rows) AS real_l1_rows,sum(a.trades) AS trades,
             sum(a.contracts) AS contracts,
             sum(a.trusted_nonstale_seconds) AS trusted_nonstale_seconds,
             sum(a.two_sided_seconds) AS two_sided_seconds,
             sum(a.weighted_spread_logodds*a.two_sided_seconds)/nullif(sum(a.two_sided_seconds),0)
               AS weighted_spread_logodds,
             sum(a.two_sided_seconds)/nullif(sum(a.trusted_nonstale_seconds),0)
               AS two_sided_share
      FROM universe u JOIN atlas_market a USING(date,market_ticker)
      WHERE u.root_map_status='PROVISIONAL_HEURISTIC_MATCHUP_TIME'
      GROUP BY u.root_event_id
    """)
    con.execute("""
      CREATE OR REPLACE TABLE atlas_sport AS
      SELECT sport,count(*) AS n_games,sum(n_market_days) AS n_market_days,
             sum(n_markets) AS n_markets,
             sum(real_l1_rows) AS rows,sum(trades) AS trades,
             avg(weighted_spread_logodds) AS mean_game_spread_logodds,
             median(weighted_spread_logodds) AS median_game_spread_logodds,
             avg(two_sided_share) AS mean_game_two_sided_share
      FROM atlas_root GROUP BY sport ORDER BY n_games DESC,sport
    """)
    con.execute(f"""
      CREATE OR REPLACE TABLE prematch_tts AS
      WITH buckets(tts_bucket,lower_s,upper_s) AS (
        VALUES ('00_0_15m',0::BIGINT,900::BIGINT),
               ('01_15_60m',900::BIGINT,3600::BIGINT),
               ('02_1_6h',3600::BIGINT,21600::BIGINT),
               ('03_6_24h',21600::BIGINT,86400::BIGINT),
               ('04_gt24h',86400::BIGINT,NULL::BIGINT)
      ), x AS (
        SELECT i.*,u.root_event_id,u.occurrence_datetime,u.sport,u.league,
               epoch_us(u.occurrence_datetime) AS occurrence_us,
               ({spread_lo}) AS spread_logodds
        FROM l1_intervals i JOIN universe u USING(date,market_ticker)
        WHERE u.root_map_status='PROVISIONAL_HEURISTIC_MATCHUP_TIME' AND i.valid_two_sided
              AND i.t_us<=epoch_us(u.occurrence_datetime)
              AND u.dim_effective_us IS NOT NULL AND i.t_us>=u.dim_effective_us
              AND i.dt_us>0
      ), overlap AS (
        SELECT x.*,b.tts_bucket,
          greatest(0,
            least(x.t_us+x.dt_us,x.occurrence_us-b.lower_s*1000000)
            - greatest(x.t_us,CASE WHEN b.upper_s IS NULL
                                   THEN -9000000000000000000
                                   ELSE x.occurrence_us-b.upper_s*1000000 END)
          ) AS bucket_dt_us
        FROM x CROSS JOIN buckets b
      )
      SELECT date,root_event_id,sport,
        tts_bucket,sum(bucket_dt_us)/1000000.0 AS trusted_nonstale_seconds,
        sum(spread_logodds*bucket_dt_us)/nullif(sum(bucket_dt_us),0)
          AS weighted_spread_logodds,
        sum((yes_ask_e4-yes_bid_e4)*cast(bucket_dt_us AS DOUBLE))/nullif(sum(bucket_dt_us),0)
          AS presentation_only_weighted_spread_e4,
        count(*) AS state_rows
      FROM overlap WHERE bucket_dt_us>0
      GROUP BY date,root_event_id,sport,tts_bucket
    """)
    tables = run_dir / "REPORT/tables"
    tables.mkdir(parents=True, exist_ok=True)
    con.execute("""
      CREATE OR REPLACE TABLE root_mapping_status_qc AS
      SELECT root_map_status,count(*) AS n_market_days,
             count(DISTINCT market_ticker) AS n_markets,
             count(DISTINCT root_event_id) AS n_games
      FROM universe GROUP BY root_map_status ORDER BY root_map_status
    """)
    con.execute("""
      CREATE OR REPLACE TABLE ambiguous_family_root_samples AS
      SELECT f.*,u.market_ticker,u.matchup_key,u.event_title,u.market_title,
             u.occurrence_datetime
      FROM family_root_qc f JOIN universe_candidates u
        ON u.date=f.date AND u.canonical_event_ticker=f.family_event_id
      WHERE f.candidate_roots>1 ORDER BY f.date,f.family_event_id,u.market_ticker
      LIMIT 100
    """)
    stable_orders = {
        "atlas_sport": "sport",
        "atlas_root": "root_event_id",
        "atlas_market": "date,market_ticker",
        "prematch_tts": "date,root_event_id,sport,tts_bucket",
        "root_mapping_status_qc": "root_map_status",
        "ambiguous_family_root_samples": "date,family_event_id,market_ticker",
        "dim_cast_qc": "field",
        "fact_market_identity_qc": "date,market_ticker",
        "dim_market_key_qc": "date,market_ticker",
    }
    for table, order in stable_orders.items():
        con.execute(
            f"COPY (SELECT * FROM {table} ORDER BY {order}) TO "
            f"{quote(str(tables / (table+'.csv')))} (HEADER,DELIMITER ',')"
        )
    total_markets = scalar(con, "SELECT count(*) FROM sports_market_ids")
    mapped_markets = scalar(con, "SELECT count(*) FROM universe WHERE root_map_status='PROVISIONAL_HEURISTIC_MATCHUP_TIME'")
    result = {
        "study_id": "STUDY-SPORT-ATLAS-01",
        "status": "DIAGNOSTIC_ONLY",
        "labels": {"data": "SEALED_DEGRADED_EVIDENCE", "timestamp": "MIXED",
                   "split": "EXPLORATORY_ONLY", "artifact": "DIAGNOSTIC_ONLY"},
        "mapping_timestamp_detail": "MIXED: fact observations use TL1 receive clocks; root/title/start mappings come from post-hoc daily snapshots and are not causal unless dim_effective_us gating is explicitly applied.",
        "tl1_receive_clock_market_days_raw": scalar(con, "SELECT count(*) FROM fact_market_identity_qc"),
        "identity_clean_market_days": total_markets,
        "distinct_identity_clean_markets": scalar(con, "SELECT count(DISTINCT market_ticker) FROM sports_market_ids"),
        "market_days_provisional_root": mapped_markets,
        "provisional_root_mapping_share": mapped_markets / total_markets if total_markets else 0,
        "root_events_provisional": scalar(con, "SELECT count(*) FROM atlas_root"),
        "root_mapping_status": rows_as_dicts(con, "SELECT * FROM root_mapping_status_qc ORDER BY root_map_status"),
        "ambiguous_family_root_count": scalar(con, "SELECT count(*) FROM family_root_qc WHERE candidate_roots>1"),
        "dimension_occurrence_cast_failures": scalar(con, "SELECT coalesce(sum(cast_failures),0) FROM dim_cast_qc"),
        "fact_identity_conflict_market_days": scalar(con, "SELECT count(*) FROM fact_market_identity_qc WHERE identity_variants>1"),
        "dimension_conflict_market_days": scalar(con, "SELECT count(*) FROM dim_market_key_qc WHERE critical_variants>1"),
        "state_duration_contract": "Each received L1 state is trusted only until the next update, the sealed coverage end, a declared gap, or 60 seconds; longer stale intervals are not counted.",
        "sports": rows_as_dicts(con, "SELECT * FROM atlas_sport ORDER BY n_games DESC,sport"),
        "mapping_warning": "Title+occurrence roots are provisional heuristics. Unmapped, invariant-conflicting, and family-multi-root rows remain in coverage but are excluded from inference.",
    }
    write_json(run_dir / "REPORT/tables/atlas_summary.json", result)
    return result


def build_markouts(con, run_dir: Path) -> dict:
    con.execute("""
      CREATE OR REPLACE TABLE flow_thresholds AS
      SELECT u.sport,quantile_cont(t.count_e4,0.99) AS q99_count_e4,
             quantile_cont(t.count_e4,0.50) AS p50_count_e4,count(*) AS train_trades
      FROM trades_safe t JOIN universe u USING(date,market_ticker)
      WHERE t.date=DATE '2026-07-12'
        AND u.root_map_status='PROVISIONAL_HEURISTIC_MATCHUP_TIME'
        AND u.dim_effective_us IS NOT NULL AND t.t_us>=u.dim_effective_us
      GROUP BY u.sport HAVING count(*)>=500
    """)
    con.execute("""
      CREATE OR REPLACE TABLE mapped_trades AS
      SELECT t.* EXCLUDE(sport),u.sport,t.sport AS fact_sport,
             u.root_event_id,u.root_map_status,u.occurrence_datetime,u.dim_effective_us,
             f.q99_count_e4,f.p50_count_e4,
             CASE WHEN t.date=DATE '2026-07-13' AND t.count_e4>=f.q99_count_e4
                  THEN true ELSE false END AS large_flow_eval
      FROM trades_safe t JOIN universe u USING(date,market_ticker)
      LEFT JOIN flow_thresholds f ON f.sport=u.sport
      WHERE u.root_map_status='PROVISIONAL_HEURISTIC_MATCHUP_TIME'
    """)
    con.execute(f"""
      CREATE OR REPLACE TABLE trade_pre AS
      SELECT t.*,b.t_us AS book_t_us,b.yes_bid_e4,b.yes_ask_e4,
             b.yes_bid_qty_e4,b.yes_ask_qty_e4
      FROM (SELECT * FROM mapped_trades ORDER BY market_ticker,t_us) t
      ASOF LEFT JOIN (SELECT * FROM l1_real ORDER BY market_ticker,t_us) b
        ON t.market_ticker=b.market_ticker AND t.t_us>b.t_us
      WHERE b.t_us IS NOT NULL AND t.t_us-b.t_us<={BOOK_AGE_CAP_US}
        AND t.dim_effective_us IS NOT NULL AND t.t_us>=t.dim_effective_us
        AND b.yes_bid_e4>0 AND b.yes_ask_e4<10000 AND b.yes_bid_e4<b.yes_ask_e4
    """)
    horizons = " UNION ALL ".join(
        f"SELECT {value}::BIGINT AS horizon_us" for value in HORIZONS_US
    )
    con.execute(f"CREATE OR REPLACE TABLE horizons AS {horizons}")
    con.execute("""
      CREATE OR REPLACE TABLE trade_targets AS
      SELECT p.*,h.horizon_us,p.t_us+h.horizon_us AS target_us
      FROM trade_pre p CROSS JOIN horizons h
    """)
    con.execute(f"""
      CREATE OR REPLACE TABLE trade_post AS
      SELECT t.*,b.t_us AS post_book_t_us,b.yes_bid_e4 AS post_bid_e4,
             b.yes_ask_e4 AS post_ask_e4
      FROM (SELECT * FROM trade_targets ORDER BY market_ticker,target_us) t
      JOIN l1_global_bounds d ON true
      ASOF LEFT JOIN (SELECT * FROM l1_real ORDER BY market_ticker,t_us) b
        ON t.market_ticker=b.market_ticker AND t.target_us>=b.t_us
      WHERE t.target_us<=d.last_t_us
        AND b.t_us IS NOT NULL AND t.target_us-b.t_us<={BOOK_AGE_CAP_US}
        AND b.yes_bid_e4>0 AND b.yes_ask_e4<10000 AND b.yes_bid_e4<b.yes_ask_e4
        AND NOT EXISTS (
          SELECT 1 FROM capture_gaps g
          WHERE g.start_us<t.target_us AND g.end_us>t.book_t_us
        )
    """)
    pre_mid = mid_logodds_sql("yes_bid_e4", "yes_ask_e4")
    post_mid = mid_logodds_sql("post_bid_e4", "post_ask_e4")
    future_e4 = expit_e4_sql(post_mid)
    con.execute(f"""
      CREATE OR REPLACE TABLE markout_events AS
      SELECT *,({pre_mid}) AS pre_mid_logodds,({post_mid}) AS post_mid_logodds,
             taker_sign*(({post_mid})-({pre_mid})) AS signed_move_logodds,
             CASE WHEN taker_sign=1 THEN yes_ask_e4-({future_e4})
                  ELSE ({future_e4})-yes_bid_e4 END AS hypothetical_touch_gross_markout_e4,
             CASE WHEN (taker_sign=1 AND yes_price_e4>yes_ask_e4)
                        OR (taker_sign=-1 AND yes_price_e4<yes_bid_e4)
                  THEN true ELSE false END AS strict_through_eligible_trade
      FROM trade_post
    """)
    con.execute("""
      CREATE OR REPLACE TABLE markout_summary AS
      SELECT date,sport,horizon_us,large_flow_eval,
             count(*) AS trade_horizon_rows,count(DISTINCT trade_id) AS n_trade_signals,
             count(DISTINCT market_ticker) AS n_markets,
             count(DISTINCT root_event_id) AS n_games,
             avg(signed_move_logodds) AS mean_signed_move_logodds,
             median(signed_move_logodds) AS median_signed_move_logodds,
             quantile_cont(signed_move_logodds,0.99) AS p99_signed_move_logodds,
             min(signed_move_logodds) AS min_signed_move_logodds,
             max(signed_move_logodds) AS max_signed_move_logodds,
             median(t_us-book_t_us) AS p50_pre_book_age_us,
             quantile_cont(t_us-book_t_us,0.99) AS p99_pre_book_age_us,
             max(t_us-book_t_us) AS max_pre_book_age_us,
             median(target_us-post_book_t_us) AS p50_outcome_book_age_us,
             quantile_cont(target_us-post_book_t_us,0.99) AS p99_outcome_book_age_us,
             max(target_us-post_book_t_us) AS max_outcome_book_age_us,
             avg(hypothetical_touch_gross_markout_e4)
               FILTER (WHERE strict_through_eligible_trade)
               AS mean_hypothetical_touch_gross_markout_e4,
             count(*) FILTER (WHERE strict_through_eligible_trade)
               AS strict_through_eligible_trade_horizon_rows
      FROM markout_events GROUP BY date,sport,horizon_us,large_flow_eval
      ORDER BY date,sport,horizon_us,large_flow_eval
    """)
    table_path = run_dir / "REPORT/tables/markout_summary.csv"
    con.execute(
        f"COPY (SELECT * FROM markout_summary ORDER BY date,sport,horizon_us,large_flow_eval) "
        f"TO {quote(str(table_path))} (HEADER,DELIMITER ',')"
    )
    lookahead = scalar(con, "SELECT count(*) FROM trade_pre WHERE book_t_us>=t_us")
    trade_dq = {
        "raw_rows": scalar(con, "SELECT count(*) FROM trades_raw"),
        "missing_local_receive_rows": scalar(con, "SELECT count(*) FROM trades_raw WHERE t_us IS NULL"),
        "missing_trade_id_rows": scalar(con, "SELECT count(*) FROM trades_raw WHERE trade_id IS NULL"),
        "unique_trade_ids_raw": scalar(con, "SELECT count(*) FROM trade_id_qc"),
        "conflicting_trade_ids": scalar(con, "SELECT count(*) FROM trade_id_qc WHERE economic_variants>1"),
        "duplicate_rows_removed": scalar(con, "SELECT coalesce(sum(raw_rows-1),0) FROM trade_id_qc WHERE economic_variants=1"),
        "safe_rows": scalar(con, "SELECT count(*) FROM trades_safe"),
        "invalid_or_inconsistent_rows_removed": scalar(con, "SELECT count(*) FROM trades_raw t JOIN trade_id_qc q USING(trade_id) WHERE q.economic_variants=1 AND (t.t_us IS NULL OR t.taker_sign IS NULL OR t.count_e4 IS NULL OR t.count_e4<=0 OR t.yes_price_e4 IS NULL OR t.yes_price_e4 NOT BETWEEN 1 AND 9999 OR t.no_price_e4 IS NULL OR t.no_price_e4 NOT BETWEEN 1 AND 9999 OR t.yes_price_e4+t.no_price_e4<>10000)"),
        "csv_parse_policy": "strict; ignore_errors=false",
    }
    write_json(run_dir / "REPORT/tables/trade_ingest_qc.json", trade_dq)
    result = {
        "study_ids": ["C1-SPREAD-CAPTURE-01", "C1-LARGE-FLOW-CONTINUATION-01"],
        "status": "DIAGNOSTIC_ONLY",
        "hypothesis_status": "CANDIDATE",
        "execution_status": "PARTIAL_DIAGNOSTIC_NOT_REGISTERED_HYPOTHESIS_TEST",
        "reason": "This is an all-horizon receive-clock markout diagnostic. It does not implement the frozen treatment/control, fees, latency, forced exit, matched-control balance, negative controls, or event-level economics and therefore is not a completed hypothesis test.",
        "threshold_train_date": "2026-07-12",
        "evaluation_date": "2026-07-13",
        "mapped_trade_count": scalar(con, "SELECT count(*) FROM mapped_trades"),
        "mapped_trades_excluded_before_dim_effective_time": scalar(con, "SELECT count(*) FROM mapped_trades WHERE dim_effective_us IS NULL OR t_us<dim_effective_us"),
        "mapped_trades_causal_dim_eligible": scalar(con, "SELECT count(*) FROM mapped_trades WHERE dim_effective_us IS NOT NULL AND t_us>=dim_effective_us"),
        "large_flow_eval_count": scalar(con, "SELECT count(*) FROM mapped_trades WHERE large_flow_eval"),
        "registered_horizons_us": HORIZONS_US,
        "scored_trade_horizon_rows": scalar(con, "SELECT count(*) FROM markout_events"),
        "scored_trade_signals": scalar(con, "SELECT count(DISTINCT trade_id) FROM markout_events"),
        "root_events": scalar(con, "SELECT count(DISTINCT root_event_id) FROM markout_events"),
        "lookahead_sentinel_rows": lookahead,
        "trade_ingest_qc": trade_dq,
        "strict_fill_boundary": "at-price never fills; this stage labels only hypothetical touch/trade-through eligibility and does not claim a reconstructed order lifecycle or fill",
        "economic_blockers": ["fees.verified=false", "latency placeholders", "no certified order lifecycle in this stage", "two day blocks"],
    }
    if lookahead:
        result["status"] = "INVALIDATED_BY_LEAKAGE"
        result["hypothesis_status"] = "INVALIDATED_BY_LEAKAGE"
    write_json(run_dir / "REPORT/tables/markout_result.json", result)
    return result


def family_and_l2_qc(con, run_dir: Path, inputs: dict) -> dict:
    con.execute("""
      CREATE OR REPLACE TABLE threeway_family_candidates AS
      SELECT date,family_event_id,min(root_event_id) AS root_event_id,
             min(sport) AS sport,count(DISTINCT market_ticker) AS legs,
             count(DISTINCT yes_sub_title) AS distinct_yes_roles,
             bool_and(coalesce(mutually_exclusive,false)) AS mutually_exclusive,
             string_agg(DISTINCT coalesce(event_structure,'_missing'),',') AS structures,
             count(*) FILTER (WHERE root_map_status='PROVISIONAL_HEURISTIC_MATCHUP_TIME')
               AS mapped_legs
      FROM universe WHERE family_event_id IS NOT NULL
      GROUP BY date,family_event_id
      HAVING count(DISTINCT market_ticker)=3 AND count(DISTINCT yes_sub_title)=3
    """)
    con.execute("""
      CREATE OR REPLACE TABLE l2_qc AS
      SELECT date,sport,count(*) AS rows,count(DISTINCT market_ticker) AS n_markets,
             count(*) FILTER (WHERE msg_type='snapshot') AS snapshots,
             count(*) FILTER (WHERE delta_e4<0) AS negative_deltas,
             count(*) FILTER (WHERE delta_e4>0) AS positive_deltas,
             quantile_cont(abs(delta_e4)/10000.0,0.99)
               FILTER (WHERE delta_e4 IS NOT NULL) AS p99_abs_delta_contracts
      FROM l2_all GROUP BY date,sport ORDER BY date,sport
    """)
    for table, order in (
        ("threeway_family_candidates", "date,family_event_id"),
        ("l2_qc", "date,sport"),
    ):
        con.execute(
            f"COPY (SELECT * FROM {table} ORDER BY {order}) TO "
            f"{quote(str(run_dir / 'REPORT/tables' / (table+'.csv')))} "
            "(HEADER,DELIMITER ',')"
        )
    family_result = {
        "study_id": "C1-THREEWAY-OVERROUND-01",
        "status": "DATA_STARVED",
        "candidate_three_leg_families": scalar(con, "SELECT count(*) FROM threeway_family_candidates"),
        "mutually_exclusive_candidates": scalar(con, "SELECT count(*) FROM threeway_family_candidates WHERE mutually_exclusive"),
        "reason": "Three distinct mutually-exclusive labels do not prove exhaustive payout roles; no family can enter executable economics until exhaustiveness is frozen.",
    }
    # ws_seq is scoped to the entire subscription stream, not a single market.
    # Per-market projections therefore contain legitimate jumps.  The release's
    # sealed receipt was computed from each complete raw sid stream and is the
    # authoritative sequence-quality record.
    receipt_fields = (
        "schema_version", "date", "generated_at_utc", "lines", "parse_errors",
        "markers_lost_frames", "seq_gap_events", "seq_missed_total",
        "seq_regressions", "sids_total", "sids_with_seq_gaps",
        "snapshot_re_anchors_total", "stream_restarts", "no_l2_files",
    )
    authoritative_receipts = []
    for path in inputs["l2_gap_receipts"]:
        value = json.loads(path.read_text(encoding="utf-8"))
        receipt = {key: value.get(key) for key in receipt_fields}
        receipt["artifact_sha256"] = sha256(path)
        receipt["release_id"] = path.parents[1].name
        authoritative_receipts.append(receipt)
    l2_result = {
        "study_ids": ["C1-HFOLLOW-RETREAT-01", "C1-DEPLETION-REFILL-01"],
        "status": "DIAGNOSTIC_ONLY",
        "hypothesis_status": "CANDIDATE",
        "execution_status": "NOT_RUN_SNAPSHOT_AWARE_REPLAY_REQUIRED",
        "l2_rows": scalar(con, "SELECT count(*) FROM l2_all"),
        "l2_markets": scalar(con, "SELECT count(DISTINCT market_ticker) FROM l2_all"),
        "coverage_by_sport": rows_as_dicts(con, "SELECT * FROM l2_qc ORDER BY date,sport"),
        "sequence_qc_authority": "sealed quality/l2_gaps.json computed on complete per-sid raw streams",
        "authoritative_sequence_receipts": authoritative_receipts,
        "per_market_sequence_inference_forbidden": True,
        "reason": "This core performed coverage/quality preflight only. L2 is a targeted subset and anchor/refill episodes require snapshot-aware replay; sealed full-stream receipts, not per-market ws_seq jumps, define sequence quality.",
    }
    write_json(run_dir / "REPORT/tables/family_result.json", family_result)
    write_json(run_dir / "REPORT/tables/l2_qc_result.json", l2_result)
    return {"family": family_result, "l2": l2_result}


def build_data_coverage(con, run_dir: Path, inputs: dict) -> dict:
    """Build the event-aware receive-clock coverage cube required by the mission."""
    con.execute("""
      CREATE OR REPLACE TABLE data_coverage_cube AS
      WITH observations AS (
        SELECT 'orderbooks_l1' AS channel,l.date,l.t_us,l.market_ticker,
               l.sport AS fact_sport,l.league,u.sport,u.family_event_id,u.root_event_id,
               u.root_map_status,u.occurrence_datetime,u.close_time
        FROM l1_all l LEFT JOIN universe u USING(date,market_ticker)
        UNION ALL
        SELECT 'trades',t.date,t.t_us,t.market_ticker,t.sport,t.league,
               u.sport,u.family_event_id,u.root_event_id,u.root_map_status,
               u.occurrence_datetime,u.close_time
        FROM trades_safe t LEFT JOIN universe u USING(date,market_ticker)
        UNION ALL
        SELECT 'orderbooks_full_l2',l.date,l.t_us,l.market_ticker,l.sport,l.league,
               u.sport,u.family_event_id,u.root_event_id,u.root_map_status,
               u.occurrence_datetime,u.close_time
        FROM l2_all l LEFT JOIN universe u USING(date,market_ticker)
      ), grouped AS (
        SELECT channel,date,date_trunc('hour',to_timestamp(t_us/1000000.0)) AS hour_utc,
               coalesce(sport,fact_sport,'_UNKNOWN') AS sport,
               coalesce(league,'_UNKNOWN') AS league,root_event_id,family_event_id,
               market_ticker,coalesce(root_map_status,'UNMAPPED_IDENTITY') AS root_map_status,
               count(*) AS rows,min(t_us) AS first_receive_us,max(t_us) AS last_receive_us,
               count(DISTINCT market_ticker) AS n_markets,
               count(DISTINCT root_event_id) AS n_games,
               CASE WHEN occurrence_datetime IS NOT NULL THEN 'START_PRESENT_POSTHOC_DIM'
                    ELSE 'START_MISSING' END AS lifecycle_start_status,
               CASE WHEN close_time IS NOT NULL THEN 'CLOSE_PRESENT_POSTHOC_DIM'
                    ELSE 'CLOSE_MISSING' END AS lifecycle_close_status
        FROM observations
        GROUP BY channel,date,hour_utc,coalesce(sport,fact_sport,'_UNKNOWN'),
                 coalesce(league,'_UNKNOWN'),root_event_id,family_event_id,
                 market_ticker,coalesce(root_map_status,'UNMAPPED_IDENTITY'),
                 lifecycle_start_status,lifecycle_close_status
      ), included AS (
      SELECT g.*,'SEALED_DEGRADED_EVIDENCE' AS evidence_tier,'TL1' AS timestamp_tier,
             'UNRATIFIED_FEES' AS fee_status,
             CASE WHEN g.channel='orderbooks_full_l2' THEN 'SEE_SEALED_L2_RECEIPT'
                  ELSE 'NOT_APPLICABLE' END AS sequence_status,
             (SELECT count(*) FROM capture_gaps c
              WHERE c.start_us<g.last_receive_us AND c.end_us>g.first_receive_us)
                AS missing_intervals,
             0::BIGINT AS excluded_records,
             'Included receive-clock rows; exclusions are reported in DATA_COVERAGE.json'
                AS exclusion_reason,
             NULL::BIGINT AS bytes,
             'Bytes are attributable at immutable object/channel level, not duplicated into market-hour cells'
                AS byte_attribution
      FROM grouped g
      ), trade_exclusions AS (
        SELECT r.date,r.market_ticker,min(r.sport) AS sport,min(r.league) AS league,
               count(*)-coalesce(max(s.safe_rows),0) AS excluded_records
        FROM trades_raw r LEFT JOIN (
          SELECT date,market_ticker,count(*) AS safe_rows
          FROM trades_safe GROUP BY date,market_ticker
        ) s USING(date,market_ticker)
        GROUP BY r.date,r.market_ticker
        HAVING count(*)-coalesce(max(s.safe_rows),0)>0
      ), excluded AS (
        SELECT 'orderbooks_l1' AS channel,x.date,NULL::TIMESTAMPTZ AS hour_utc,
               coalesce(u.sport,x.sport,'_UNKNOWN') AS sport,
               coalesce(x.league,'_UNKNOWN') AS league,u.root_event_id,u.family_event_id,
               x.market_ticker,coalesce(u.root_map_status,'UNMAPPED_IDENTITY') AS root_map_status,
               0::BIGINT AS rows,NULL::BIGINT AS first_receive_us,NULL::BIGINT AS last_receive_us,
               1::BIGINT AS n_markets,CASE WHEN u.root_event_id IS NULL THEN 0 ELSE 1 END::BIGINT AS n_games,
               CASE WHEN u.occurrence_datetime IS NOT NULL THEN 'START_PRESENT_POSTHOC_DIM' ELSE 'START_MISSING' END AS lifecycle_start_status,
               CASE WHEN u.close_time IS NOT NULL THEN 'CLOSE_PRESENT_POSTHOC_DIM' ELSE 'CLOSE_MISSING' END AS lifecycle_close_status,
               'SEALED_DEGRADED_EVIDENCE','MISSING_TL1_RECEIVE_CLOCK','UNRATIFIED_FEES',
               'NOT_APPLICABLE',NULL::BIGINT,x.missing_local_receive_rows,
               'Excluded: local_recv_ts_us is NULL',NULL::BIGINT,
               'Object-level bytes only'
        FROM l1_ingest_by_market x LEFT JOIN universe u USING(date,market_ticker)
        WHERE x.missing_local_receive_rows>0
        UNION ALL
        SELECT 'orderbooks_full_l2',x.date,NULL::TIMESTAMPTZ,
               coalesce(u.sport,x.sport,'_UNKNOWN'),coalesce(x.league,'_UNKNOWN'),
               u.root_event_id,u.family_event_id,x.market_ticker,
               coalesce(u.root_map_status,'UNMAPPED_IDENTITY'),0::BIGINT,NULL::BIGINT,NULL::BIGINT,
               1::BIGINT,CASE WHEN u.root_event_id IS NULL THEN 0 ELSE 1 END::BIGINT,
               CASE WHEN u.occurrence_datetime IS NOT NULL THEN 'START_PRESENT_POSTHOC_DIM' ELSE 'START_MISSING' END,
               CASE WHEN u.close_time IS NOT NULL THEN 'CLOSE_PRESENT_POSTHOC_DIM' ELSE 'CLOSE_MISSING' END,
               'SEALED_DEGRADED_EVIDENCE','MISSING_TL1_RECEIVE_CLOCK','UNRATIFIED_FEES',
               'SEE_SEALED_L2_RECEIPT',NULL::BIGINT,x.missing_local_receive_rows,
               'Excluded: local_recv_ts_us is NULL',NULL::BIGINT,'Object-level bytes only'
        FROM l2_ingest_by_market x LEFT JOIN universe u USING(date,market_ticker)
        WHERE x.missing_local_receive_rows>0
        UNION ALL
        SELECT 'trades',x.date,NULL::TIMESTAMPTZ,
               coalesce(u.sport,x.sport,'_UNKNOWN'),coalesce(x.league,'_UNKNOWN'),
               u.root_event_id,u.family_event_id,x.market_ticker,
               coalesce(u.root_map_status,'UNMAPPED_IDENTITY'),0::BIGINT,NULL::BIGINT,NULL::BIGINT,
               1::BIGINT,CASE WHEN u.root_event_id IS NULL THEN 0 ELSE 1 END::BIGINT,
               CASE WHEN u.occurrence_datetime IS NOT NULL THEN 'START_PRESENT_POSTHOC_DIM' ELSE 'START_MISSING' END,
               CASE WHEN u.close_time IS NOT NULL THEN 'CLOSE_PRESENT_POSTHOC_DIM' ELSE 'CLOSE_MISSING' END,
               'SEALED_DEGRADED_EVIDENCE','INVALID_OR_MISSING_CAUSAL_ROW','UNRATIFIED_FEES',
               'NOT_APPLICABLE',NULL::BIGINT,x.excluded_records,
               'Excluded: missing clock/id, conflicting duplicate, invalid side/size/price, or exact duplicate',
               NULL::BIGINT,'Object-level bytes only'
        FROM trade_exclusions x LEFT JOIN universe u USING(date,market_ticker)
      )
      SELECT * FROM included UNION ALL SELECT * FROM excluded
    """)
    cube_path = run_dir / "REPORT/tables/data_coverage_cube.csv"
    con.execute(
        f"COPY (SELECT * FROM data_coverage_cube ORDER BY date,hour_utc,channel,sport,league,"
        f"coalesce(root_event_id,''),coalesce(family_event_id,''),market_ticker) TO "
        f"{quote(str(cube_path))} (HEADER,DELIMITER ',')"
    )
    object_rows = sorted(inputs["object_bindings"], key=lambda row: (
        row["release_id"], row["channel"], row["key"]
    ))
    write_json(run_dir / "DATA_INTEGRITY/CONSUMED_OBJECTS.json", {
        "selection": "exact release MANIFEST keys and VersionIds only",
        "objects": object_rows,
        "rfq_overlap_objects_deduplicated": inputs["rfq_duplicate_objects_skipped"],
    })
    inventory_rows = sorted(inputs["manifest_inventory"], key=lambda row: (
        row["release_id"], row["inventory_class"], row["key"]
    ))
    inventory_totals: dict[tuple[str, str], dict[str, int | str]] = {}
    for row in inventory_rows:
        aggregate = inventory_totals.setdefault(
            (row["release_id"], row["inventory_class"]),
            {
                "release_id": row["release_id"],
                "inventory_class": row["inventory_class"],
                "objects": 0,
                "bytes": 0,
            },
        )
        aggregate["objects"] = int(aggregate["objects"]) + 1
        aggregate["bytes"] = int(aggregate["bytes"]) + int(row.get("bytes") or 0)

    def compact_file_lists(value):
        """Keep integrity metadata readable; exact file bindings live in inventory_rows."""
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                if key == "files" and isinstance(child, list):
                    result["files_summary"] = {
                        "count": len(child),
                        "bytes": sum(int(item.get("size") or 0)
                                     for item in child if isinstance(item, dict)),
                        "exact_bindings": "DATA_INTEGRITY/MANIFEST_INVENTORY.json",
                    }
                else:
                    result[key] = compact_file_lists(child)
            return result
        if isinstance(value, list):
            return [compact_file_lists(item) for item in value]
        return value

    release_integrity = []
    for release in inputs["releases"]:
        manifest = release["manifest"]
        marker = release["marker"]
        release_integrity.append({
            "release_id": release["release_id"],
            "date": manifest.get("date"),
            "evidence_tier": manifest.get("evidence_tier"),
            "evidence_tier_basis": manifest.get("evidence_tier_basis"),
            "publication_state_sha256": manifest.get("publication_state_sha256"),
            "tl1_status": manifest.get("tl1_status"),
            "seal": compact_file_lists(manifest.get("seal")),
            "version_binding": compact_file_lists(manifest.get("version_binding")),
            "corrections": compact_file_lists(manifest.get("corrections")),
            "channels": compact_file_lists(manifest.get("channels")),
            "publication_state": compact_file_lists(manifest.get("publication_state")),
            "verification_marker": compact_file_lists(marker),
        })
    manifest_inventory_path = run_dir / "DATA_INTEGRITY/MANIFEST_INVENTORY.json"
    write_json(manifest_inventory_path, {
        "schema_version": "sports-autoresearch-manifest-inventory-v1",
        "selection": "Every object in both immutable release MANIFESTs; analytic readers consume only explicitly classified inputs.",
        "objects": inventory_rows,
        "totals": [inventory_totals[key] for key in sorted(inventory_totals)],
        "releases": release_integrity,
    })
    object_channel_totals: dict[str, dict[str, int | str]] = {}
    for row in object_rows:
        channel = row["channel"]
        aggregate = object_channel_totals.setdefault(
            channel, {"channel": channel, "objects": 0, "bytes": 0}
        )
        aggregate["objects"] = int(aggregate["objects"]) + 1
        aggregate["bytes"] = int(aggregate["bytes"]) + int(row.get("bytes") or 0)
    coverage = {
        "schema_version": "sports-autoresearch-coverage-v2",
        "coverage_status": "PARTIAL_CORE_FACT_CUBE_RFQ_FULL_STAGE_PENDING",
        "labels": {
            "data_evidence": "SEALED_DEGRADED_EVIDENCE",
            "timestamp_discipline": "TL1 facts; MIXED for post-hoc dimension mapping",
            "split": "EXPLORATORY_ONLY", "artifact": "DIAGNOSTIC_ONLY",
        },
        "coverage_cube": "REPORT/tables/data_coverage_cube.csv",
        "coverage_cube_cells": scalar(con, "SELECT count(*) FROM data_coverage_cube"),
        "channels": rows_as_dicts(con, """
          SELECT channel,sum(rows) AS included_rows,count(DISTINCT market_ticker) AS n_markets,
                 count(DISTINCT root_event_id) AS n_games,count(DISTINCT date) AS n_day_blocks,
                 min(first_receive_us) AS first_receive_us,max(last_receive_us) AS last_receive_us
          FROM data_coverage_cube GROUP BY channel ORDER BY channel
        """),
        "dates": [str(row[0]) for row in con.execute(
            "SELECT DISTINCT date FROM data_coverage_cube ORDER BY date").fetchall()],
        "input_objects": {
            "count": len(object_rows),
            "bytes": sum(int(row.get("bytes") or 0) for row in object_rows),
            "by_channel": [object_channel_totals[key]
                           for key in sorted(object_channel_totals)],
        },
        "manifest_inventory": {
            "path": "DATA_INTEGRITY/MANIFEST_INVENTORY.json",
            "sha256": sha256(manifest_inventory_path),
            "objects": len(inventory_rows),
            "bytes": sum(int(row.get("bytes") or 0) for row in inventory_rows),
            "by_release_and_class": [inventory_totals[key]
                                     for key in sorted(inventory_totals)],
            "release_integrity": release_integrity,
        },
        "exclusions": {
            "l1_rows_missing_local_receive_clock": scalar(con, "SELECT coalesce(sum(missing_local_receive_rows),0) FROM l1_ingest_qc"),
            "l2_rows_missing_local_receive_clock": scalar(con, "SELECT coalesce(sum(missing_local_receive_rows),0) FROM l2_ingest_qc"),
            "trade_rows_missing_local_receive_clock": scalar(con, "SELECT count(*) FROM trades_raw WHERE t_us IS NULL"),
            "trade_rows_missing_trade_id": scalar(con, "SELECT count(*) FROM trades_raw WHERE trade_id IS NULL"),
            "fact_identity_conflict_market_days": scalar(con, "SELECT count(*) FROM fact_market_identity_qc WHERE identity_variants>1"),
            "dimension_conflict_market_days": scalar(con, "SELECT count(*) FROM dim_market_key_qc WHERE critical_variants>1"),
            "trade_conflicting_ids": scalar(con, "SELECT count(*) FROM trade_id_qc WHERE economic_variants>1"),
            "trades_removed_by_validation_or_dedup": scalar(con, "SELECT count(*) FROM trades_raw") - scalar(con, "SELECT count(*) FROM trades_safe"),
            "mapped_trades_before_dim_effective": scalar(con, "SELECT count(*) FROM mapped_trades WHERE dim_effective_us IS NULL OR t_us<dim_effective_us"),
        },
        "capture_gaps": rows_as_dicts(con, "SELECT * FROM capture_gaps ORDER BY start_us,end_us"),
        "l2_sequence_receipts": [
            {"release_id": path.parents[1].name, "object_key": "quality/l2_gaps.json",
             "sha256": sha256(path),
             "summary": {key: json.loads(path.read_text(encoding='utf-8')).get(key)
                         for key in ("date","lines","seq_gap_events","seq_missed_total",
                                     "seq_regressions","sids_total","stream_restarts")}}
            for path in inputs["l2_gap_receipts"]
        ],
        "limitations": [
            "This is the core fact cube. Full RFQ row/channel coverage is appended by the separately preregistered RFQ full stage before mission completion.",
            "Market-hour bytes are intentionally NULL to avoid duplicating immutable object bytes.",
            "Root/family mappings are post-hoc snapshot heuristics; causal studies separately apply dim_effective_us.",
            "Fee facts remain unratified and cannot support net-economics gates.",
        ],
    }
    write_json(run_dir / "DATA_COVERAGE.json", coverage)
    markdown = [
        "# DATA COVERAGE", "", f"> {BANNER}", "",
        f"Coverage cube: `{coverage['coverage_cube']}` ({coverage['coverage_cube_cells']:,} cells).", "",
        "The cube is grouped by UTC hour, sport, league, provisional root event, family, market and channel.",
        "Facts use TL1 receive clocks. Root/title/start dimensions are post-hoc snapshots unless a causal study applies `dim_effective_us`.", "",
        "## Channel totals", "",
    ]
    markdown.extend(
        f"- {row['channel']}: {row['included_rows']:,} rows; {row['n_markets']:,} markets; "
        f"{row['n_games']:,} provisionally mapped games; {row['n_day_blocks']} day blocks"
        for row in coverage["channels"]
    )
    (run_dir / "DATA_COVERAGE.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    return coverage


def charts(con, run_dir: Path) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    chart_dir = run_dir / "REPORT/charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    generated = []
    sports = rows_as_dicts(con, """
      SELECT sport,n_games,median_game_spread_logodds,mean_game_two_sided_share
      FROM atlas_sport WHERE n_games>=2 ORDER BY n_games DESC LIMIT 20
    """)
    if sports:
        labels = [r["sport"] for r in sports]
        values = [float(r["median_game_spread_logodds"] or 0) for r in sports]
        fig, ax = plt.subplots(figsize=(12, 7))
        ax.barh(labels[::-1], values[::-1], color="#4da3ff")
        ax.set_xlabel("Median root-event time-weighted spread (log-odds)")
        ax.set_title("Sports atlas — mapped root events only\n" + BANNER)
        ax.grid(axis="x", alpha=.25)
        fig.tight_layout()
        path = chart_dir / "atlas_spread_by_sport.png"
        fig.savefig(path, dpi=150)
        plt.close(fig)
        generated.append(str(path.relative_to(run_dir)))
    for name in (
        "C1-ANOM-RFQ-SIZE-TAIL-01__trigger_ccdf.png",
        "C1-ANOM-RFQ-LIFECYCLE-MIXTURE-01__trigger_survival.png",
    ):
        path = chart_dir / name
        if not path.is_file():
            raise DeepResearchError(f"preregistered RFQ trigger reproduction missing: {path}")
        generated.append(str(path.relative_to(run_dir)))
    return generated


def update_run_artifacts(run_dir: Path, results: dict, input_identity: dict, elapsed: float) -> None:
    manifest_path = run_dir / "RUN_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["analysis_started"] = True
    manifest["cycle1_core_completed_at_utc"] = now()
    manifest["status"] = "CYCLE1_CORE_COMPLETE_RFQ_FULL_SCAN_PENDING"
    manifest["environment"] = {
        "python": platform.python_version(), "platform": platform.platform(),
        "duckdb": input_identity["duckdb"], "threads": input_identity["threads"],
        "memory_limit": input_identity["memory_limit"],
    }
    manifest["cycle1_core_wall_seconds"] = round(elapsed, 3)
    manifest["cycle1_core_input_binding"] = {
        "consumed_manifest_objects": input_identity["consumed_manifest_objects"],
        "rfq_overlap_objects_deduplicated": input_identity["rfq_overlap_objects_deduplicated"],
        "selection_rule": "Only exact MANIFEST object keys; overlapping RFQ keys require identical SHA-256 and are counted once.",
    }
    ledger_path = run_dir / "HYPOTHESIS_LEDGER.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    result_map = {
        "C1-SPREAD-CAPTURE-01": results["markout"],
        "C1-LARGE-FLOW-CONTINUATION-01": results["markout"],
        "C1-PREMATCH-TTS-01": {
            "status": "DIAGNOSTIC_ONLY", "hypothesis_status": "CANDIDATE",
            "execution_status": "PARTIAL_TTS_ATLAS_ONLY_FULL_TEST_NOT_RUN",
            "atlas_tts_table": "REPORT/tables/prematch_tts.csv",
        },
        "C1-THREEWAY-OVERROUND-01": results["family_l2"]["family"],
        "C1-HFOLLOW-RETREAT-01": results["family_l2"]["l2"],
        "C1-DEPLETION-REFILL-01": results["family_l2"]["l2"],
        "C1-SOCCER-POISSON-RV-01": {"status": "DATA_STARVED", "reason": "Cross-family same-game mapping is not authoritative at current dim coverage."},
        "C1-RFQ-CLOB-01": {"status": "DIAGNOSTIC_ONLY", "hypothesis_status": "CANDIDATE", "execution_status": "PENDING_NOT_RUN", "reason": "full RFQ extraction stage pending"},
        "C1-ANOM-RFQ-SIZE-TAIL-01": {"status": "DIAGNOSTIC_ONLY", "hypothesis_status": "CANDIDATE", "execution_status": "TRIGGER_ONLY_DEPENDENT_TEST_NOT_RUN", "trigger_plot_created": True, "dependent_test_opened": False},
        "C1-ANOM-RFQ-LIFECYCLE-MIXTURE-01": {"status": "DIAGNOSTIC_ONLY", "hypothesis_status": "CANDIDATE", "execution_status": "TRIGGER_ONLY_DEPENDENT_TEST_NOT_RUN", "trigger_plot_created": True, "dependent_test_opened": False},
    }
    for card in ledger["hypotheses"]:
        if card["hypothesis_id"] in result_map:
            card["cycle1_result"] = result_map[card["hypothesis_id"]]
            card["status"] = result_map[card["hypothesis_id"]].get(
                "hypothesis_status", result_map[card["hypothesis_id"]].get("status", card["status"])
            )
    write_json(ledger_path, ledger)
    summary = {
        "run_id": manifest["run_id"], "banner": BANNER,
        "atlas": results["atlas"], "markout": results["markout"],
        "family": results["family_l2"]["family"], "l2": results["family_l2"]["l2"],
        "coverage": {
            "cube": results["coverage"]["coverage_cube"],
            "cells": results["coverage"]["coverage_cube_cells"],
            "channels": results["coverage"]["channels"],
        },
        "charts": results["charts"],
        "boundary": "No result is PROMOTION_READY or a formal verdict; full RFQ stage remains pending.",
    }
    write_json(run_dir / "REPORT/CYCLE1_CORE_SUMMARY.json", summary)
    md = [
        f"# SPORTS-AUTORESEARCH Cycle-1 core — {manifest['run_id']}", "",
        f"> **{BANNER}**", "",
        "## Current conclusion", "",
        "The first deep exploratory core completed. Mapping/atlas and receive-clock",
        "markout diagnostics are real results, but the evidence cannot support",
        "promotion or a formal strategy verdict. The full RFQ scan is still pending.", "",
        "## Status", "",
        f"- Atlas: `{results['atlas']['status']}`; provisional heuristic root events: {results['atlas']['root_events_provisional']}",
        f"- Spread/large flow: `{results['markout']['status']}`; trade×horizon rows: {results['markout']['scored_trade_horizon_rows']}",
        f"- Three-way family: `{results['family_l2']['family']['status']}`",
        f"- H-FOLLOW/depletion L2: `{results['family_l2']['l2']['status']}`",
        "- RFQ full lifecycle/CLOB extraction: `PENDING`", "",
        "## Highest-priority gaps", "",
        "1. Historical dim-market mapping covers only a small share of fact markets.",
        "2. Only two day blocks exist, both degraded pending PIPE-W03 quality assessment.",
        "3. Exact fees and order latency are not ratified for binding economics.",
        "4. Cross-family root-event and exhaustive three-way role maps are incomplete.", "",
    ]
    (run_dir / "REPORT/FULL_REPORT.md").write_text("\n".join(md), encoding="utf-8")
    (run_dir / "REPORT/EXECUTIVE_SUMMARY.md").write_text("\n".join(md[:18]), encoding="utf-8")
    registry_path = run_dir / "TRIAL_REGISTRY.jsonl"
    existing_stage_trials = set()
    for line in registry_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get("stage") == "CYCLE1_CORE":
                existing_stage_trials.add(row.get("trial_id"))
    with registry_path.open("a", encoding="utf-8") as handle:
        for card in ledger["hypotheses"]:
            trial_id = card["hypothesis_id"]
            if trial_id in existing_stage_trials:
                continue
            result = result_map.get(trial_id, {})
            handle.write(json.dumps({
                "recorded_at_utc": manifest["cycle1_core_completed_at_utc"],
                "trial_id": trial_id,
                "stage": "CYCLE1_CORE",
                "record_type": "RESULT_STAGE_APPEND",
                "result_opened": result.get("execution_status") not in (
                    "PENDING_NOT_RUN", "NOT_RUN_SNAPSHOT_AWARE_REPLAY_REQUIRED"
                ),
                "hypothesis_status": result.get(
                    "hypothesis_status", result.get("status", card["status"])
                ),
                "artifact_status": result.get("status", "DIAGNOSTIC_ONLY"),
                "execution_status": result.get("execution_status", "COMPLETED_OR_DATA_SCREENED"),
                "query_sha256": manifest["repository"]["query_set_sha256"],
                "source_manifest_sha256": manifest["repository"]["source_manifest_sha256"],
                "release_ids": [item["release_id"] for item in manifest["selected_releases"]],
                "result_artifact": "REPORT/CYCLE1_CORE_SUMMARY.json",
            }, sort_keys=True) + "\n")
    # The manifest is the commit record for this stage.  Write it last so a
    # report/ledger failure cannot advertise an incomplete stage as complete.
    write_json(manifest_path, manifest)


def run(args) -> None:
    started = time.time()
    run_dir = Path(args.run_dir).resolve()
    if not (run_dir / "RUN_MANIFEST.json").is_file():
        raise DeepResearchError("run manifest missing")
    manifest = json.loads((run_dir / "RUN_MANIFEST.json").read_text(encoding="utf-8"))
    if manifest.get("run_id") != run_dir.name:
        raise DeepResearchError("run id/path mismatch")
    if manifest.get("mode") != "EXPLORATORY_AUTORESEARCH":
        raise DeepResearchError("run is not MODE-1 exploratory")
    if manifest.get("status") != "CYCLE1_CORE_RUNNING" or not manifest.get("analysis_started"):
        raise DeepResearchError("registration/gate attestation was not completed before analysis")
    gates = manifest.get("gates", {})
    if not str(gates.get("gate_a", {}).get("status", "")).startswith("PASS_"):
        raise DeepResearchError("Gate A same-run verification is not PASS")
    if not str(gates.get("gate_b", {}).get("status", "")).startswith("PASS_"):
        raise DeepResearchError("Gate B W09 attestation is not PASS")
    attestation_path = run_dir / "DATA_INTEGRITY/W09_ATTESTATION.json"
    if not attestation_path.is_file():
        raise DeepResearchError("W09 attestation artifact missing")
    attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    if (attestation.get("instance_id") != EXPECTED_INSTANCE or
            attestation.get("region") != EXPECTED_REGION or
            attestation.get("role") != EXPECTED_ROLE or
            attestation.get("instance_profile") != EXPECTED_ROLE or
            attestation.get("w09_run_inhibitor_present") is not True or
            attestation.get("w09_run_inhibitor_is_ancestor") is not True or
            attestation.get("static_credentials_present") is not False or
            attestation.get("trading_credentials_present") is not False or
            attestation.get("ambient_aws_or_kalshi_variables") != [] or
            attestation.get("static_credential_paths_present") != [] or
            attestation.get("installation_sha256") != EXPECTED_W09_INSTALLATION_SHA256 or
            attestation.get("s3_access") != "READ_ONLY_RESEARCH_PREFIX"):
        raise DeepResearchError("W09 identity/inhibitor attestation mismatch")
    expected_attestation_sha = gates.get("gate_b", {}).get("attestation_sha256")
    if not expected_attestation_sha or sha256(attestation_path) != expected_attestation_sha:
        raise DeepResearchError("Gate B is not bound to the current W09 attestation bytes")
    inputs = discover_release_inputs(Path(args.cache_root).resolve())
    selected = {item["release_id"]: item for item in manifest.get("selected_releases", [])}
    for release in inputs["releases"]:
        frozen = selected.get(release["release_id"])
        local_manifest = release["base"] / "MANIFEST.json"
        if not frozen or sha256(local_manifest) != frozen.get("manifest_sha256"):
            raise DeepResearchError(f"local release manifest is not bound to frozen run: {release['release_id']}")
    import duckdb
    if duckdb.__version__ != "1.4.5" or attestation.get("duckdb") != "1.4.5":
        raise DeepResearchError(
            f"DuckDB version drift (runner={duckdb.__version__}, attested={attestation.get('duckdb')})"
        )
    db_path = run_dir / "cache/cycle1.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    configure(con, run_dir, args.memory_limit, args.threads, args.max_temp_directory_size)
    try:
        make_views(con, inputs)
        materialize_dimensions(con, inputs)
        atlas = build_atlas(con, run_dir)
        markout = build_markouts(con, run_dir)
        family_l2 = family_and_l2_qc(con, run_dir, inputs)
        coverage = build_data_coverage(con, run_dir, inputs)
        generated_charts = charts(con, run_dir)
        results = {"atlas": atlas, "markout": markout,
                   "family_l2": family_l2, "coverage": coverage,
                   "charts": generated_charts}
        update_run_artifacts(run_dir, results, {
            "duckdb": duckdb.__version__, "threads": args.threads,
            "memory_limit": args.memory_limit,
            "consumed_manifest_objects": len(inputs["object_bindings"]),
            "rfq_overlap_objects_deduplicated": len(inputs["rfq_duplicate_objects_skipped"]),
        }, time.time() - started)
    finally:
        con.close()
    print(f"CYCLE1_CORE_COMPLETE run_id={run_dir.name} wall_seconds={time.time()-started:.3f}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--cache-root", default="/srv/w09-research/cache")
    parser.add_argument("--memory-limit", default="40GB")
    parser.add_argument("--max-temp-directory-size", default="120GB")
    parser.add_argument("--threads", type=int, default=8)
    args = parser.parse_args(argv)
    if args.threads < 1:
        parser.error("threads must be positive")
    try:
        run(args)
    except Exception as exc:
        print(f"CYCLE1_CORE_BLOCKED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
