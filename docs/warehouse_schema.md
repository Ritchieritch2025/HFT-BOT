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
3. **ARCHIVE** — a completed day is eligible for sealed research only after
   every closed raw byte has an equal ingest checkpoint, the exporter writes
   final partitioned files (sorted, zstd-15 Parquet for orderbooks; csv.gz for
   trades), and an exact `EXCEPT ALL` proof finds zero content differences in
   either direction between staging and every archive partition. `--seal` then
   atomically writes `seals/date=<D>.json` (version 2, `method=full_v2`),
   binding the dated `manifest.csv` digest, the producing `code_commit`, the
   exact closed-raw inventory (SHA-256 + checkpoint, verified ONCE at seal
   time) and per-archive-file SHA-256+MD5. **Seals are WRITE-ONCE** (operator
   ruling 2026-07-11): `--seal` on a sealed day is a verify-only no-op; a
   failing existing seal requires the explicit
   `--operator-invalidate-seal <reason>` procedure, which PARKS the seal
   (never deletes) and ledgers the reason. Raw is prunable AFTER sealing
   (`tools/prune_raw.py`, seal-gated, fail-closed): readers verify the
   ARCHIVE against the seal only, never local raw. Late facts for a sealed
   day NEVER enter staging or mutate the day — ingest diverts them verbatim
   to `corrections/date=<D>/late_rows.ndjson` (+ `corrections/ledger.ndjson`
   count + ALERT); the seal stays byte-identical. Pre-seal-system history is
   sealed once via `--legacy-seal` (`method=legacy_v0`): archive
   self-consistency only, unverified items named in the seal,
   `go_no_go_eligible=false` FOREVER (operator ruling 2026-07-11 option A).
   Write-once files without a seal are
   not a complete research source. The archive root (`ARCHIVE_ROOT` in
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
open_interest_e4, is_snapshot` + the four W-TL1 ladder columns (below).
**trades**: `ts_utc, market_ticker, series_ticker, event_ticker, category,
subcategory, group, trade_id, yes_price_e4, no_price_e4, count_e4, taker_side`
+ ladder columns.
**orderbooks_full** (watchlist runs): `ts_utc, market_ticker, series_ticker,
event_ticker, category, subcategory, group, msg_type ('snapshot'|'delta'),
side, price_e4, delta_e4, yes_levels, no_levels, ws_sid, ws_seq` + ladder
columns.
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

### Timestamp ladder (added 2026-07-10, W-TL1 — additive, nullable)
All three fact tables carry four nullable BIGINT columns, filled from the raw
capture envelope at ingest:

| column | meaning |
|---|---|
| `exchange_ts_us` | exchange-reported time, epoch µs. `ts_ms` is AUTHORITATIVE (ms×1000); legacy `ts` is fallback only (JSON number = epoch seconds, JSON string = ISO-8601); anything else NULL — no unit guessing. **Kalshi exchange timestamps are millisecond-granular; sub-ms conclusions are not supported.** |
| `recv_wall_ns` | capture-host wall clock at WS receive — raw envelope value, untransformed |
| `recv_mono_ns` | capture-host monotonic clock at WS receive — raw envelope value; arbitrary epoch, differences meaningful only within one `stream_epoch`/connection |
| `local_recv_ts_us` | `recv_wall_ns // 1000` — **the tradable decision clock** (when WE saw the message) |

**`ts_utc` is legacy-compat only**: `ts_utc = COALESCE(exchange_ts_us,
local_recv_ts_us)`. It remains the partition/export/coarse-query key for old
tools, but it is EXCHANGE time on most rows — using it as a backtest replay
clock is look-ahead bias. `tools/mm_backtest.py` defaults to `--clock recv`
(local_recv_ts_us, fail-closed on missing) and allows `--clock exchange` only
as a look-ahead-bias diagnostic (never go/no-go, Q2).

**Scheduled heartbeats** (system state-continuation rows, not exchange
messages): `ts_utc` = hour start ALWAYS (export/LOCF/heartbeat detection
depend on it), all four ladder columns NULL ALWAYS (no fabricated
timestamps). Heartbeats may seed LOCF continuation but are never a "strategy
newly saw the market change" trigger, enter no lag/jitter/residual statistic,
and are counted separately by every consumer.

**History**: existing archive files are NEVER rewritten (write-once, D1);
pre-TL1 files read back NULL in all four columns via `load()`'s
union_by_name (an all-NULL csv.gz ladder column is dtype-pinned back to
BIGINT in `load()`). A real historical backfill is W-TL2 (rebuild from
vaulted raw + row-count/key-level diff), not a join-style patch.

The lag/jitter/pacing-residual diagnostic over raw envelopes is
`work/research/jitter_report.py` (reads raw NDJSON ONLY — reconnect
boundaries don't exist in warehouse tables).

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
columns, ffill, archive_only)` routes archive vs staging. `archive_only=True`
requires every bounded UTC date to have a valid seal whose manifest digest is
still current; an unsealed/partial all-market day raises `UnsealedDayError`
(a RuntimeError subclass — deliberately NOT FileNotFoundError, so "no data,
skip" handlers cannot swallow it) instead of silently omitting lagging
partitions. `warehouse.last_seal_grades()` exposes per-day seal methods after
a load: `legacy_v0` days are permanently go/no-go ineligible and every
consumer must surface them (banner/report label). The four research tools
(mm_scan/mm_backtest/mm_calibrate/mm_research) are HARD archive-only: any
range reaching today is refused up front and live staging is unreachable
from them (PIPE-R001 — the 2026-07-11 ingest-starvation root cause). Entering archive-only mode closes any cached live
staging attachment before creating the relation. Default mixed mode routes
today/unexported days through staging transparently. A bounded window ending
before today's UTC boundary automatically selects sealed archive-only; explicit
`archive_only=False` is a diagnostic/migration escape hatch, not an unattended
research setting. The loader never double-counts the
overlap day. The archive/staging
dedup is PER `(category, subcategory)` partition, not a global max-archive-date
(AF-3): each partition carries its own max archived date, and a staging row is
excluded only when its OWN partition archived through its day. This is correct
for every query shape — pinned category+subcategory, category-only
(`subcategory=None`), and all-categories (`category=None`) — so a partition that
archives on a later day than another (naturally: a sparse partition with no rows
on a day has no archive dir and trails) never drops the laggard's not-yet-
archived staging rows on the boundary day. Staging rows are matched to their
sanitized partition (mirroring the archive dir names) via the SQL replica of
`warehouse_common.sanitize`. `ffill=True` applies LOCF.

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
missing ⇒ NULL, pre-W5 staging migrated on init, old+new archives union ·
W-TL1 ladder (tests/test_ingest.py §10, tests/test_timestamp_ladder.py,
tests/test_backtest_clock.py, tests/test_jitter_report.py): four columns flow
raw→staging→export, heartbeats all-NULL + hour-start ts_utc, pre-TL1
staging/archives migrate/union to NULL, exchange-clock fake profit vs
recv-clock zero-fill demonstrated on a late-arrival fixture.

## Event packs (derived layer — PLAN_EVENT_PACKAGING)
A **derived, rebuildable** layer under `work/event_packs/`, keyed by
`event_ticker` (or `market_ticker` for market-unit categories), so any market /
series / single event can be selected and validated regardless of UTC-midnight
splits. Raw + archive stay time-partitioned (D1); event packs never change them.
- `index.parquet` (W-E1): one row per packaging unit with an inferred
  `[win_start_us, win_end_us]` window (observed activity + lifecycle, never
  scheduled `close_time` alone) + seal state.
- `data/unit=<key>/{trades,orderbooks_l1}.csv` + `manifests/<key>.json` (W-E2):
  the materialized pack. **Money-integrity: E4 integer columns carried
  byte-exact, no float / no sub-penny loss (D5, AF-1/2).**
- `warehouse.load(event=<key>, index_path=…)` resolves the unit's window +
  market set from `index.parquet` and returns exactly that episode across day
  partitions — callers pass no calendar dates (three-axis selector, §3.5).
- `event_validate.py` (W-E3) stamps `pass | degraded | fail`. **V-EP15 (AF-1):
  a pack whose window overlaps a capture gap is `degraded`, NEVER `pass`** —
  day-granular / self-consistency checks (V-EP1/2/10/12) cannot see a sub-day
  hole. A `degraded` pack must not feed a Q2 bound.

## Reserved (documented, not built this phase)
`facts/game_data/` + `game_kalshi_map` for sports enrichment (external game state
→ Kalshi market join).
