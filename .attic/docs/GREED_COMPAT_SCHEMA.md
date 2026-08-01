# Greed-Compatible Research Warehouse

> **SUPERSEDED (2026-07-06).** The dump/convert cycle documented here
> (`tools/warehouse_convert_capture.py`) was removed and replaced by the
> three-layer warehouse — see `docs/warehouse_schema.md`. The old Greed-layout
> partitions were parked under `work/warehouse/legacy_greed/` by
> `tools/migrate_warehouse.py`. This file is kept for the Greed field-mapping
> reference only.

The local research warehouse is a cold-path system for Kalshi market data:

```text
Kalshi WS / REST capture
    -> raw NDJSON truth source
    -> offline converter
    -> Greed-compatible Parquet partitions
    -> DuckDB research queries
```

Raw `WsRecorder` capture NDJSON remains the truth source. Parquet files are
derived state and must be rebuildable from raw capture.

## Cold-Path Boundary

DuckDB is allowed only for offline research warehouse tooling. It is forbidden
in `tradingd`, `ws_client`, `OrderBookManager`, strategy, execution, risk, and
any live-order code.

The warehouse converter does not connect to Kalshi, place orders, run risk
checks, or participate in WebSocket read-loop latency. It reads local files and
writes local Parquet only.

If DuckDB is missing, warehouse tools/tests fail clearly with:

```text
DuckDB is required for the offline research warehouse.
```

No silent SQLite or alternate warehouse fallback is allowed for this phase.

## Public Schema

The pinned public schema is:

```text
docs/vendor/greed/schema_snapshot.json
```

Public Greed-compatible tables:

```text
orderbooks_l1
orderbooks_full
trades
markets
events
market_settlements
rfq_events
```

Public tables contain only the columns in the schema snapshot, in snapshot
order. Internal metadata is kept out of public Parquet rows and lives under:

```text
work/warehouse/_meta/warehouse_ingest_runs.json
work/warehouse/_meta/warehouse_source_offsets.json
work/warehouse/_meta/warehouse_schema_versions.json
work/warehouse/_meta/warehouse_partition_manifest.json
work/warehouse/_meta/warehouse_time_sources.json
```

## Partition Layout

The converter writes day-partitioned Parquet:

```text
work/warehouse/orderbooks_l1/date=YYYY-MM-DD/part-00000.parquet
work/warehouse/orderbooks_full/date=YYYY-MM-DD/part-00000.parquet
work/warehouse/trades/date=YYYY-MM-DD/part-00000.parquet
work/warehouse/markets/date=YYYY-MM-DD/part-00000.parquet
work/warehouse/events/date=YYYY-MM-DD/part-00000.parquet
work/warehouse/market_settlements/date=YYYY-MM-DD/part-00000.parquet
```

`rfq_events` remains in the schema snapshot, but RFQ ingestion is off by
default.

## Mapping Rules

Prices and quantities are serialized as fixed-point decimal strings. The
converter avoids Python float conversion for prices, quantities, counts,
volumes, and open interest.

`orderbooks_full` stores YES and NO bid arrays in their original economic form.
YES asks are derived from the best NO bid and are not stored in
`orderbooks_full`.

`orderbooks_l1` and `orderbooks_full` emit only changed book states, skip
consecutive duplicates, and enforce a one-second minimum interval per market.

`trades.taker_side` keeps the Greed public column name, but the value prefers
Kalshi `taker_outcome_side`; legacy `taker_side` is used only when the outcome
field is absent.

Timestamp fallback to receive wall clock is recorded in
`warehouse_time_sources.json`; public tables do not grow `*_source` columns.

## Rebuild

```bash
python3 tools/warehouse_convert_capture.py \
  --input work/e2e_.../capture.ndjson \
  --warehouse work/warehouse \
  --run-id e2e_... \
  --replace-run
```

The converter also reads rotated capture shards named `capture.ndjson.1`,
`capture.ndjson.2`, and so on.

DuckDB view definitions for research queries live in:

```text
sql/duckdb_greed_schema.sql
```
