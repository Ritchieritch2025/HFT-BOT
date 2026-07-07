# Kalshi Warehouse — Schema & Design (locked)

Grounded in the live Kalshi API (verified). All timestamps UTC; day boundary =
UTC midnight. Correct API usage is the top priority — every field maps to a real
API field; nothing is invented.

## Layers
1. **CAPTURE** — the firehose (`ws_shadow` `KALSHI_WS_FIREHOSE=1`, driven by
   `tools/pipeline_supervisor.sh`) appends each WS message as one JSON line to
   the **hourly raw log** `work/raw/date=<YYYY-MM-DD>/firehose_<HH>.ndjson`
   (UTC). Append-only (restarts within an hour keep appending), 3-day
   retention. Channels: `ticker` (L1) + `trade`, no market filter → all markets.
2. **STAGING** — `tools/ingest.py --loop` tails the raw logs (~60s cycle),
   resolves the join, applies the class policy + change-only + heartbeats,
   writes typed rows into `work/warehouse/staging.duckdb`. A
   `checkpoint(file, byte_offset)` table makes restarts gap/dup-free (only
   complete lines are processed; a trailing partial line waits for the next
   cycle). Today is queryable live. Staging retains today + 1 prior day.
3. **ARCHIVE** — at UTC midnight `tools/export_day.py` exports the completed
   day to final partitioned files (sorted, zstd-15 Parquet for orderbooks;
   csv.gz for trades), verifies row counts by re-reading every file, appends
   `manifest.csv` + `compression_report.csv`, and prunes staging. Write-once,
   complete and final — no part files. The archive root (`ARCHIVE_ROOT` in
   `config/warehouse.yaml`, env-overridable) may be an external drive: the
   exporter probes it is mounted/writable first; if not, staging is retained,
   the failure is alerted, and the export retries next cycle.

## Hierarchy (single global standard)
```
category → subcategory → group → series_ticker → event_ticker → market_ticker
```
- `category`      = `series.category` (18 live categories).
- `subcategory`   = `series.tags[0]` ("_none" if empty). Full tags kept in the dim. **Pinned** in `catalog/series_classified` — never recomputed at ingest (partition-stable).
- `group`         = **derived** league / region / asset. Kalshi has **no** league field, so: Sports→league (MLB, KBO…), Weather→region (city), Crypto→asset (=tag). Low-confidence derivations are flagged in `config/classification_review.csv` for manual curation. `group_source` records how each was derived.
- `series/event/market` — Kalshi ticker convention: `market_ticker = {event}-{outcome}`, `event = {series}-{event_id}`. Derived by string split at ingest (works even for markets the catalog hasn't seen yet).

## Two-class policy (`config/market_classes.yaml`)
- **Class A** (orderbooks_l1 change-only + heartbeats, ALL markets in category):
  Crypto, Financials, Economics, Climate and Weather, Commodities, Sports, Politics.
- **Class B** (NO L1): Companies, Education, Elections, Entertainment, Exotics,
  Health, Mentions, Science and Technology, Social, Transportation, World.
- **trades + settlements are recorded for EVERY market regardless of class.**
- `orderbooks_full`: watchlist tickers only.
- Category not in config → treated as Class B + a logged warning.

## Change-only recording (orderbooks_l1)
State per market = `(yes_bid_e4, yes_bid_qty_e4, yes_ask_e4, yes_ask_qty_e4)`.
- first observation of a market → write, `is_snapshot=true`
- new clock-hour since last snapshot → write **heartbeat**, `is_snapshot=true`
- state changed within the hour → write, `is_snapshot=false`
- otherwise → skip
Restart rebuilds state from the last row per market in staging.
**Reconstruction:** book at time T = the most recent row for that market at or
before T (last-observation-carried-forward). Max lookback = 1 hour. `load(..., ffill=True)` applies this.
Note: `volume_e4` / `open_interest_e4` are carried as-of the last **book** change
(they move on trades); use the `trades` table as the volume source of truth.

## Types (locked — no downcasting; Kalshi is sub-penny + fractional)
| kind | type | encoding | why |
|---|---|---|---|
| price | INT32 | dollars × 10000 (E4) — `"0.0090"→90` | 21% of trades are sub-penny; UINT8 cents would lose them |
| quantity | BIGINT | size × 10000 (E4) — `"5119.00"→51190000` | 68% of sizes are fractional; UINT32 would truncate |
| timestamp | BIGINT | epoch **microseconds**, UTC | |
| is_snapshot | BOOLEAN | heartbeat/first = true, change = false | |

## Tables
**orderbooks_l1** (staging + archive): `ts_utc, market_ticker, series_ticker,
event_ticker, category, subcategory, group, record_class, yes_bid_e4,
yes_bid_qty_e4, yes_ask_e4, yes_ask_qty_e4, price_e4, volume_e4,
open_interest_e4, is_snapshot`.
**trades**: `ts_utc, market_ticker, series_ticker, event_ticker, category,
subcategory, group, trade_id, yes_price_e4, no_price_e4, count_e4, taker_side`.
**orderbooks_full** (watchlist runs): `ts_utc, market_ticker, series_ticker,
event_ticker, category, subcategory, group, msg_type ('snapshot'|'delta'),
side, price_e4, delta_e4, yes_levels, no_levels, ws_sid, ws_seq`.
**dim** (`catalog/`): `series`, `events`, `markets`, `settlements` (raw, all
API fields) + `series_classified` (pinned category/subcategory/group/class).

### ws_sid / ws_seq (added 2026-07-07, W5 — additive, nullable)
`orderbook_snapshot` / `orderbook_delta` WS frames carry a top-level `sid`
(subscription id) and `seq` (per-sid monotonic sequence; snapshots share the
sid's counter) — verified against live captures (W3.3). The ingester stores
them as nullable BIGINTs; frames without them (and every pre-2026-07-07 row
or archive) read back NULL — the columns are never required. On init the
ingester ALTERs them into a pre-W5 staging table (instant, nullable, safe);
the exporter's `SELECT *` carries them into the parquet automatically, and
`load()` unions old (13-col) and new archives by name, missing ⇒ NULL.
**Known ordering defect this fixes:** `orderbooks_full` had no sequence
column, so same-µs deltas were ordered only by raw-file position. With
`ws_seq`, per-sid gap detection and true delta ordering are possible. The
gold builder's merge (PLAN_GOLD_DATA_CONTRACT W2.3) can adopt `ws_seq` as
its sequence source in a future workstream — adoption is out of W5 scope;
until then gold builds on pre-W5 data record `seq_unavailable`.

## Directory & naming (archive)
```
<ARCHIVE_ROOT>/<table>/category=<C>/subcategory=<S>/date=<YYYY-MM-DD>/
    <table>__<C>__<S>__<YYYY-MM-DD>.parquet    orderbooks_l1, orderbooks_full
    <table>__<C>__<S>__<YYYY-MM-DD>.csv.gz     trades, settlements
```
Hive-style key=value folders; filenames repeat C/S/date so every file is
self-describing on its own. Path values are sanitized (spaces → `_`, e.g.
`Climate and Weather` → `Climate_and_Weather`) identically by the exporter and
`load()`; the real value is always a column inside the file. Parquet is zstd
level 15, sorted by `(market_ticker, ts_utc)` before write (sorted data
compresses far better). Settlements partition by settlement date; capture ts
kept as a column.

## manifest.csv (warehouse root)
One row per archived file: `date, table, category, subcategory, row_count,
file_path, file_md5, created_ts`. `compression_report.csv` logs per-day
per-category `raw_ticks_seen` vs `rows_written_after_dedup` (from the
`ingest_stats` staging table) — visibility into the change-only compression.

## Heartbeat scheduler (in addition to lazy per-tick heartbeats)
Whenever the ingester's clock (max data ts; wall clock too in `--loop` mode)
crosses an hour boundary, every **active-session** market (a tick seen within
`heartbeat_active_hours`, default 24h) that lacks a snapshot for that hour gets
a heartbeat row from remembered state: `ts_utc` = hour start,
`is_snapshot=true`, book fields only (`price/volume/oi` are NULL on scheduled
heartbeats). This guarantees a market with zero ticks for N hours still gets N
heartbeat rows, so LOCF lookback stays ≤ 1 hour. After a restart the active set
is re-seeded from the last row per market; markets that died right before the
restart may receive up to `heartbeat_active_hours` of extra heartbeats — bounded
and harmless.

## Access — one entry point
`tools/warehouse.py::load(table, category, subcategory, group, start, end,
columns, ffill)` routes archive (past days) vs staging (today / unexported
days) transparently and never double-counts the overlap day (any day present
in the archive is excluded from the staging scan). `ffill=True` applies LOCF.

## Dim snapshots (daily)
`tools/dim_snapshot.py` dual-writes `dim/snapshots/date=<D>/{series,events,
markets}.csv` + `dim/latest/` from the catalog parquets. `markets` gains two
derived fields: `event_structure` (bracket | binary | multi_outcome |
head_to_head | cumulative — inferred from per-event market count, numeric
strikes, and `event.mutually_exclusive`) and `bracket_rank` (strike-ordered
position within bracket events; null otherwise).

## Migration
`tools/migrate_warehouse.py` (one-time, idempotent): re-ingests legacy raw
captures into staging, parks the removed Greed-layout partitions under
`warehouse/legacy_greed/`, regenerates the classification + per-series tags
report (`config/series_tags_report.csv`), and exports completed days.

## Acceptance (all passing — tests/test_ingest.py, tests/test_export_day.py)
change-only (100 identical + 1 change → 2 rows) · heartbeat (3 quiet hours → 3
`is_snapshot` rows at hour starts) · E4 types preserve sub-penny/fractional ·
kill/restart mid-file → counts reconcile, no dups · Class B has trades but no
L1 · manifest rows == archive files, md5 verified · staging prune retains
today + 1 prior day · load() slices by category/subcategory/date and routes
archive vs staging automatically · ws_sid/ws_seq flow raw→staging→export,
missing ⇒ NULL, pre-W5 staging migrated on init, old+new archives union.

## Reserved (documented, not built this phase)
`facts/game_data/` + `game_kalshi_map` for sports enrichment (external game state
→ Kalshi market join).
