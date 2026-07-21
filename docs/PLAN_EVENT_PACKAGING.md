# PLAN — Event-Centric Packaging (derived research layer)

**For the executing agent: read `docs/GUARDRAILS.md` and `docs/MM_ROADMAP.md`
before touching anything. This plan binds to them; a conflict means STOP and
ask the operator.**

- **Phase advanced:** 1.5 (research substrate — per-event calibration,
  backtests, and gate scans without calendar-day fragmentation).
- **Gates:** P1 (phase named), P2 (no live-trading reach), P3 (bounded scope,
  demonstrated acceptance, rollback below), P4 (pipeline continuity — capture/
  ingest/export **untouched**; event packs are a **derived, rebuildable**
  layer under `work/event_packs/`), E1/E2/E5 (tests + docs in the same change).
- **Safety class of everything in this plan:** read-only research + derived
  exports. Nothing touches order paths, ws_shadow, or supervisor. No live_order
  tools.

---

## 0. Problem statement (operator requirement, 2026-07-07)

Calendar-day archive partitions (`date=<YYYY-MM-DD>`) are correct for ops,
backup, and midnight export — but **wrong as the unit of research** for
time-bounded real-world events (sports matches, debate nights, macro releases)
that span UTC midnight. A tennis match from 23:00 UTC day D to 01:30 UTC day
D+1 is split across two archive folders; `mm_backtest --date D` sees only half
the episode. The fix is **not** to repartition raw capture or daily export
(D1, P4). The fix is a **fourth derived layer**: event packs keyed by
`event_ticker`, with automatically inferred `[window_start, window_end]`,
materialized from calendar archives + staging + catalog dim.

### 0.1 Operator product requirements (binding)

The operator must be able to:

1. **Pick any single market** (`market_ticker`) and run tests on that market's
   full active window — not a calendar day slice.
2. **Pick any series** (`series_ticker`) and run tests across all events in that
   series (batch), with per-event results broken out.
3. **Pick any single event** (`event_ticker`) and run tests on that episode
   with **complete** L1 + trades for **every market** belonging to the event
   (head-to-head, bracket, multi-outcome) — including data that crosses UTC
   midnight.
4. **Export CSVs** where events are **visually and structurally separated**,
   filenames self-describing, and a sidecar manifest proves completeness
   (row counts, markets included, window, `crossed_day_boundary`).

Day/hour storage stays underneath (D1). The **user-facing unit** is
`market | event | series`, not `date`.

### 0.2 Lifecycle timing fork (accuracy for game end)

Scheduled `close_time` shifts with OT/delays (vendor spec). Production
firehose today captures only `ticker,trade` — **no** `market_lifecycle_v2`
(`ws_shadow.cpp` firehose channels). Accurate `determination_ts` requires
**Fork A** (add lifecycle channel to capture — separate W-LC). Until then,
**Fork B** uses observed first/last tick + catalog, marks
`window_source=observed_merged`, and never claims determination accuracy.

Window-end priority (both forks, best available wins):

```
t_determined (WS lifecycle) → t_settled (WS) → t_last_seen (warehouse)
  → scheduled_close (catalog, reference only)
```

---

## 1. Grounding facts (measured / verified in-repo)

| Fact | Value / source |
|---|---|
| Archive partition key | `date=<YYYY-MM-DD>` per `docs/warehouse_schema.md` §Directory |
| Row identity for grouping | `event_ticker` on every L1/trade row (ingest-derived from ticker split) |
| Kalshi time fields | `open_time`, `close_time` on **Market** (not EventData) — `openapi.yaml` Market schema |
| Event metadata | `EventData`: `event_ticker`, `series_ticker`, `strike_date`, `mutually_exclusive` — no open/close |
| Catalog refresh | `catalog_sync.py` hourly (full markets every 6h) + `dim_snapshot.py` daily CSV |
| Existing event grouping | `gate_calc.py` groups L1 by `event_ticker` but input is **single-day** `load(..., start=day, end=day)` |
| Gold `market_id` | Day-scoped per `PLAN_GOLD_DATA_CONTRACT` — event packs do **not** change gold layout in this plan |
| Q7 filter | Drop `KXMVE*` / combo legs before packaging (same as gate_calc) |
| Q6 | `close_time` must be read for window inference and surfaced when missing (fail-closed widen + WARN) |

---

## 2. Scope

**In:**

- `config/event_packaging.yaml` — per-category packaging unit + padding rules.
- `tools/event_index.py` — build/refresh `work/event_packs/index.parquet` (one
  row per packable unit with inferred window, status, market list).
- `tools/event_pack.py` — materialize sealed packs from warehouse (read-only).
- `tools/event_validate.py` — accuracy + robustness checks (V-EP1..V-EP14).
- `tools/event_export.py` — CSV exports per §3.6 (`--market | --event | --series`).
- `warehouse.load(..., market=..., event=..., series=...)` — three-axis query.
- `mm_backtest --market | --event | --series`; `gate_calc --event` (read paths).
- `tools/event_lifecycle.py` + capture channel add (Fork A, W-LC only).
- Tests, fixtures, `tools.json` entries, `docs/warehouse_schema.md` amendment
  (§Event packs), RUNBOOK one-liner.

**Out (separate plans):**

- Changing raw hourly logs, `export_day.py` partition keys, or supervisor loop.
- Gold builder event-scoped `market_id` (future `PLAN_GOLD_EVENT` if needed).
- Live trading / quote windows (Phase 2 uses `close_time` on hot path separately).
- Historical REST backfill (`MASTER_SEQUENCE` STEP 5).

---

## 3. Design

### 3.1 Packaging units (not everything is an "event")

`config/event_packaging.yaml` (new):

```yaml
# unit: event | market | skip
# pre_pad_us / post_pad_us: microseconds added around inferred bounds
defaults:
  unit: event
  pre_pad_us: 3600000000      # 1h
  post_pad_us: 3600000000      # 1h
  seal_after_close_us: 7200000000   # 2h quiet after max(close_time) → sealed
  min_ticks_to_seal: 1

categories:
  Sports:        { unit: event, pre_pad_us: 7200000000, post_pad_us: 3600000000 }
  Politics:      { unit: event }
  Crypto:        { unit: market, pre_pad_us: 300000000, post_pad_us: 600000000 }
  Financials:    { unit: event }
  Economics:     { unit: event, pre_pad_us: 14400000000, post_pad_us: 7200000000 }
  Exotics:       { unit: skip }   # Q7 — never MM; still skippable for packs
  # unset category → defaults.unit
```

- **`event` unit:** one pack per `event_ticker`; includes **all** markets sharing
  that ticker (bracket / head-to-head / multi-outcome).
- **`market` unit:** one pack per `market_ticker` (short-dated crypto).
- **`skip`:** no pack; index row `status=excluded` with reason.

### 3.2 Window inference algorithm (observed + lifecycle, not close_time)

For each packaging unit `U` at index-build time `T_build`:

**Inputs:** `dim/latest/markets.csv` (+ `events.csv` for ME flag), optional
`dim/snapshots/date=*` for as-of replay; warehouse facts for observed bounds;
raw firehose lifecycle frames (Fork A only) for `determination_ts`/`settled_ts`.

**Step A — market set**

- `event` unit: all markets where `event_ticker = U`.
- `market` unit: singleton `{U}`.

**Step B — scheduled bounds** (reference only — NOT used alone to seal)

```
sched_start = min(open_time) over markets in set   # ISO → µs
sched_close = max(close_time) over markets in set  # may change mid-game
```

Missing fields → `catalog_incomplete=true`; do **not** invent times.

**Step C — lifecycle bounds** (Fork A; nullable on Fork B)

Per market in set, from raw `market_lifecycle_v2` frames:

```
t_determined = max(determination_ts) where event_type=determined
t_settled    = max(settled_ts) where event_type=settled
```

**Step D — observed bounds** (always computed from warehouse)

```
t_first_seen = min(ts_utc) over L1 ∪ trades for markets in set
t_last_seen  = max(ts_utc) over L1 ∪ trades for markets in set
```

Scan all archive `date=` partitions overlapping
`[sched_start, sched_close] ± 2d` (or ±7d if catalog missing), plus staging.
If no rows → `observed_empty=true`.

**Step E — merged window** (what packs and exports actually use)

```
win_start = min(sched_start, t_first_seen) - pre_pad   # drop sched_* if missing
win_end   = max(t_determined, t_settled, t_last_seen, sched_close) + post_pad
```

`window_source` records which bound set `win_end` (e.g. `determined`,
`last_seen`, `scheduled_close`). If only observed: `observed_merged`.

**Step F — divergence report** (D2 — never silent)

If `t_last_seen` exceeds `sched_close` by >2h, or `t_first_seen` precedes
`sched_start` by >2h → `window_divergence=true` (common for sports OT).

**Step G — seal state**

| status | Meaning |
|---|---|
| `scheduled` | catalog window in future; no observed ticks |
| `active` | now ∈ [win_start, win_end] or ticks in last `seal_after_close_us` |
| `partial` | past win_end but markets not all `settled` in latest catalog |
| `sealed` | all markets settled OR quiet period elapsed after close |
| `excluded` | Q7 / policy skip |
| `failed` | validation failed on last pack attempt |

Seal transition: when `status` would be `partial` and
`now > max(close_time) + seal_after_close_us` and no new ticks in that period →
`sealed` (pack job may run).

### 3.3 Event pack layout (derived, rebuildable)

```
work/event_packs/
  index.parquet                    # authoritative index (§3.4 schema)
  manifests/
    <unit_key>.json                # unit_key = event_ticker or market_ticker
  data/
    unit=<sanitized_key>/
      orderbooks_l1.parquet
      trades.csv.gz
      dim_markets.csv              # markets in unit + close_time + structure
      dim_event.csv                # event row if unit=event
```

`unit_key` sanitization: same rules as archive path (spaces → `_`).

**manifest.json** (required fields):

```json
{
  "unit": "event",
  "unit_key": "KXATPMATCH-25JUL07ABC",
  "window_start_us": 1720382400000000,
  "window_end_us": 1720391400000000,
  "window_source": "merged",
  "catalog_incomplete": false,
  "window_divergence": false,
  "markets": ["...-A", "...-B"],
  "tables": {
    "orderbooks_l1": {"row_count": 12345, "file": "...", "md5": "..."},
    "trades": {"row_count": 678, "file": "...", "md5": "..."}
  },
  "archive_days_spanned": ["2026-07-07", "2026-07-08"],
  "built_at_us": ...,
  "builder_git_rev": "...",
  "validation": {"status": "pass", "checks": ["V-EP1", "..."]}
}
```

Packs are **idempotent**: re-run `event_pack.py --unit KEY` replaces `data/` +
manifest atomically (write tmp → rename).

### 3.4 Index schema (`index.parquet`)

| column | type | description |
|---|---|---|
| unit | string | `event` \| `market` |
| unit_key | string | `event_ticker` or `market_ticker` |
| category | string | from series dim |
| subcategory | string | tags[0] |
| group | string | derived league/asset |
| event_ticker | string | self or parent |
| markets | string[] | member market tickers |
| series_ticker | string | parent series |
| sched_start_us | bigint | nullable (catalog open_time) |
| sched_close_us | bigint | nullable (catalog close_time; reference) |
| t_first_seen_us | bigint | nullable |
| t_last_seen_us | bigint | nullable |
| t_determined_us | bigint | nullable (Fork A) |
| t_settled_us | bigint | nullable (Fork A) |
| win_start_us | bigint | |
| win_end_us | bigint | |
| window_source | string | what set win_end |
| crossed_day_boundary | bool | t_first/t_last on different UTC dates |
| status | string | §3.2 G |
| catalog_incomplete | bool | |
| window_divergence | bool | |
| pack_path | string | nullable |
| pack_built_at_us | bigint | nullable |
| pack_row_l1 | bigint | nullable |
| pack_row_trades | bigint | nullable |

### 3.5 Three-axis selector (market · event · series)

Every research/export tool accepts **exactly one primary axis** (plus optional
filters). The index resolves windows; callers never pass calendar dates for
episode tests.

| Axis | Key | Window | Rows included |
|---|---|---|---|
| **Market** | `--market TICKER` | index row where `unit=market` and `unit_key=TICKER` | that market only |
| **Event** | `--event TICKER` | index row where `unit=event` and `unit_key=TICKER` | **all** markets with that `event_ticker` |
| **Series** | `--series TICKER` | union of windows for all index rows with `series_ticker=TICKER` and `status∈{active,sealed}` | all markets in those events; results **per event** |

Series batch mode never merges events into one timeline — it loops events and
emits one result block (and one CSV folder) per `event_ticker`.

`warehouse.load()` filters (all combinable with axis):

```python
load("trades", market="KX...-YES")           # single market, auto window
load("orderbooks_l1", event="KX...MATCH", ffill=True)
load("trades", series="KXATPMATCH", status="sealed")
```

Implementation: read `index.parquet` → resolve window(s) → expand archive
`date=` files across spanned days → filter tickers + `win_start ≤ ts ≤ win_end`.

**Must not** double-count staging vs archive (archived days final; staging = tail).

### 3.6 CSV export contract (operator clarity)

Tool: `tools/event_export.py` (W-E7). Writes human-inspectable CSVs under:

```
work/event_exports/
  <series_ticker>/
    <event_ticker>/
      _manifest.json              # completeness proof (required)
      _event_summary.csv          # one row: title, window, markets, n_trades/n_l1 ROW counts (label "not contract volume"; add integer contracts_e4=SUM(count_e4) for volume — AF-4)
      orderbooks_l1.csv             # ALL markets in event, sorted ts_utc
      trades.csv
      markets/                    # optional per-market split
        <market_ticker>__l1.csv
        <market_ticker>__trades.csv
  by_market/
    <market_ticker>/
      _manifest.json
      orderbooks_l1.csv
      trades.csv
```

**Column rules (every data CSV):**

- Leading columns always: `event_ticker`, `series_ticker`, `market_ticker`,
  `category`, `subcategory`, `group`, `ts_utc`, `time_utc` (human ISO).
- **MONEY-INTEGRITY (D5, audit AF-1/AF-2/AF-3 — BLOCKS W-E2/W-E7):** every
  price/size/quantity column is carried as its **authoritative E4 fixed-point
  integer** (`yes_bid_e4`, `yes_ask_e4`, `yes_bid_qty_e4`, `yes_ask_qty_e4`;
  trades `yes_price_e4`, `no_price_e4`, `count_e4`) — always present, byte-exact
  vs the warehouse. A dollar-decimal string MAY be added **only** as a labeled
  readability duplicate, derived by INTEGER arithmetic to exactly 4 dp
  (`f"{e4//10000}.{abs(e4)%10000:04d}"` or `Decimal(e4)/10000`) — **never float
  division, never rounded to 2 dp** (sub-penny is real: `0.0090` must not become
  `0.01`). Do NOT reuse `export_day.py STRATEGY_COLS` (it floats `e4/10000.0` and
  drops the `_e4` columns). W-E2/W-E7 acceptance asserts the E4 integer columns
  are present and byte-exact.
- L1: `yes_bid_e4, yes_ask_e4, yes_bid_qty_e4, yes_ask_qty_e4` (+ optional
  4dp-integer dollar/qty strings for readability).
- Trades: `trade_id, yes_price_e4, no_price_e4, count_e4, taker_side`
  (`count_e4` = contract quantity, E4 — NOT a row count; + optional readability
  strings).
- **Naming (AF-3):** `count_e4` is contract quantity; row cardinality is
  `row_count`/`n_trades` — never overload "count" for both.
- **No mixing events in one file** except explicit `--series --combined` flag
  (default OFF). Default = one folder per event.

**`_manifest.json` required fields:**

```json
{
  "selector": {"axis": "event", "key": "KX..."},
  "series_ticker": "KXATPMATCH",
  "event_ticker": "KX...",
  "markets": ["...", "..."],
  "window_start_utc": "2026-07-07T23:00:00Z",
  "window_end_utc": "2026-07-08T01:30:00Z",
  "crossed_day_boundary": true,
  "window_source": "last_seen",
  "archive_days_spanned": ["2026-07-07", "2026-07-08"],
  "row_counts": {"orderbooks_l1": 12345, "trades": 678},
  "completeness": "pass",
  "validation_checks": ["V-EP1", "V-EP12"]
}
```

`completeness=pass` only if `event_validate.py` PASS for that unit.

**Operator commands (target UX):**

```sh
# 单一事件 → 一个文件夹，CSV 完整
python3 tools/event_export.py --event KXATPMATCH-25JUL07ABC

# 单一市场
python3 tools/event_export.py --market KXATPMATCH-25JUL07ABC-PLAYER1

# 整个系列赛 → 每个事件一个子文件夹
python3 tools/event_export.py --series KXATPMATCH --status sealed

# 回测（自动用 event 窗口，不用 --date）
python3 tools/mm_backtest.py --event KXATPMATCH-25JUL07ABC
python3 tools/mm_backtest.py --series KXATPMATCH --limit-events 20
python3 tools/mm_backtest.py --market KX...-PLAYER1
```

---

## 4. Query integration

### 4.1 `warehouse.load()` extension

See §3.5. Delivers the programmatic API behind exports and backtests.

### 4.2 Research CLIs

- `mm_backtest.py --market | --event | --series` (pessimistic/optimistic unchanged)
- `mm_calibrate.py --event` (optional W-E6)
- `gate_calc.py --event` (single-episode mode)
- `event_export.py` (W-E7 — CSV deliverable)

---

## 5. Workstreams (seven-field Ws)

### W-E0 — Measurement + policy doc
Purpose:          quantify calendar-split pain on real warehouse; land policy YAML.
Allowed reads:    staging + archive + dim (read-only).
Allowed writes:   `config/event_packaging.yaml`; `tools/event_measure_split.py`
                  (read-only report); `tests/fixtures/event_split_cases.csv`
                  (operator-curated cross-midnight examples); this plan §3.1 values.
Forbidden writes: pipeline, export, ingest, capture.
Acceptance:       `event_measure_split.py --days 7` prints: count of Sports events
                  with `obs_start` and `obs_end` on different UTC dates; top-20
                  by tick count with split row counts day D vs D+1; CSV written to
                  `work/event_packs/split_report_<date>.csv`.
Rollback:         revert commit.
Exit evidence:    commit hash; report path + headline numbers in SESSION_LOG.

### W-E1 — Event index builder
Purpose:          `event_index.py` implements §3.2 A–F; writes `index.parquet`.
Allowed reads:    dim, catalog, warehouse load() (read-only).
Allowed writes:   `tools/event_index.py`; `tests/test_event_index.py`;
                  `tests/fixtures/event_index_catalog/` (synthetic dim CSVs);
                  `work/event_packs/index.parquet` (derived).
Forbidden writes: pipeline; warehouse.py (W-E3 owns load extension).
Acceptance:       fixture catalog → deterministic index rows; Q7 exclusions;
                  `catalog_incomplete` and `window_divergence` flags on seeded cases;
                  property: `win_start <= obs_start` and `win_end >= obs_end` whenever
                  observed non-empty.
Rollback:         revert commit; delete `work/event_packs/`.
Exit evidence:    commit hash; pytest green; sample index printed for 3 fixtures.

### W-E2 — Pack materializer
Purpose:          `event_pack.py` builds `data/unit=...` + manifest from index row.
Allowed reads:    index, warehouse load() (read-only).
Allowed writes:   `tools/event_pack.py`; `tests/test_event_pack.py`;
                  `tests/fixtures/event_pack/` (tiny synthetic archives);
                  `work/event_packs/data/**`, `manifests/**`.
Forbidden writes: archive_root (read-only); staging writes.
Money-integrity:  carries E4 integer columns byte-exact (§3.6 AF-1/AF-2/AF-3) —
                  NO float price/size, NO 2dp rounding, dollar strings (if any)
                  are 4dp integer-derived. Do NOT reuse export_day.STRATEGY_COLS.
Stale-window (AF-5): pack ONLY `sealed` units, and RE-INFER the window at pack
                  time (never trust a stale index `win_end`) — an `active`/
                  `partial` unit whose `t_last` advanced after index build would
                  otherwise clip late trades.
Acceptance:       synthetic two-day archive → one event pack; row counts match
                  direct SQL filter; manifest md5 matches files; rebuild is
                  bit-identical (idempotent); E4 integer columns present + byte-
                  exact vs warehouse (anti-float assert); a unit with trades
                  AFTER the stored index `win_end` is refused-or-refreshed, never
                  silently clipped (AF-5 regression case).
Rollback:         revert commit; delete derived packs.
Exit evidence:    commit hash; pytest green; one manifest path.

### W-E3 — Validator + load() integration
Purpose:          `event_validate.py` (V-EP*) + `warehouse.load(event=...)`.
Allowed reads:    packs, index, archive (read-only).
Allowed writes:   `tools/event_validate.py`; `tools/warehouse.py`; `tests/test_event_validate.py`;
                  `tests/test_warehouse_event.py`; `docs/warehouse_schema.md` (§Event packs).
Forbidden writes: export_day, ingest, capture.
Acceptance:       all V-EP checks (§6) green on fixtures; `load(..., event=...)` passes
                  cross-midnight fixture; existing `load()` day mode unchanged (regression).
Rollback:         revert commit.
Exit evidence:    commit hash; validation matrix printed; schema doc diff.

### W-E4 — Research tool wiring (three-axis)
Purpose:          `mm_backtest --market|--event|--series`, `gate_calc --event`;
                  registry + runbook.
Allowed reads:    event packs / load(market=|event=|series=...).
Allowed writes:   `tools/mm_backtest.py`; `tools/gate_calc.py`; `tools.json`;
                  `docs/RUNBOOK.md` (append); `tests/test_mm_backtest_event.py`.
Forbidden writes: strategy math (log-odds/fees unchanged — Q1/Q3).
Acceptance:       cross-midnight fixture: `--date D` alone FAILS completeness check;
                  `--event` / `--market` run full window; `--series` emits per-event
                  result blocks (no merged timeline); gate_calc `--event` matches manual
                  two-day concat.
Rollback:         revert commit.
Exit evidence:    commit hash; test output; runbook diff.

### W-E7 — CSV export (`event_export.py`)
Purpose:          operator-facing CSV bundles per §3.6; one folder per event by default.
Allowed reads:    index, packs or load() (read-only).
Allowed writes:   `tools/event_export.py`; `tests/test_event_export.py`;
                  `tests/fixtures/event_export/`; `work/event_exports/**`.
Forbidden writes: archive, capture, ingest.
Acceptance:       fixture cross-midnight event: export folder has `_manifest.json` with
                  `completeness=pass`, `crossed_day_boundary=true`, both days in
                  `archive_days_spanned`; `orderbooks_l1.csv` + `trades.csv` row counts
                  match V-EP1; **no row in CSV has a different `event_ticker`** except
                  `--series` per-event subfolders; series export creates N event
                  subfolders not one combined file (default).
Rollback:         revert commit; delete `work/event_exports/`.
Exit evidence:    commit hash; sample export tree path; V-EP14 PASS.

### W-LC — Lifecycle capture (Fork A; operator-gated)
Purpose:          add `market_lifecycle_v2` to firehose channels; parse
                  `determination_ts`/`settled_ts` for accurate game-end windows.
Allowed reads:    raw firehose; vendor asyncapi.
Allowed writes:   `apps/ws_shadow.cpp` OR supervisor env `KALSHI_WS_CHANNELS`;
                  `tools/event_lifecycle_scan.py` (raw JSONL parser);
                  `tests/test_event_lifecycle.py`; `tests/test_ingest_lifecycle.py`
                  OR raw-only scan test (D4: capture change + test same commit);
                  `docs/warehouse_schema.md` (optional `lifecycle_events` table note).
Forbidden writes: export_day partition keys; trading paths.
Acceptance:       mock/raw fixture with `determined` frame → index row gains
                  `t_determined_us`; production capture continuity statement in commit;
                  firehose still records ticker+trade (no regression); ingest test proves
                  lifecycle frames either land in raw (minimum) or typed staging (stretch).
Rollback:         revert commit; rebuild ws_shadow; pipeline reload.
Exit evidence:    commit hash; raw sample with lifecycle type; index row before/after.

### W-E5 — Daily pack job (supervisor hook proposal)
Purpose:          seal + pack newly settled events; refresh index incrementally.
Allowed reads:    index, catalog, warehouse.
Allowed writes:   `tools/event_pack_daily.py`; `tests/test_event_pack_daily.py`;
                  `docs/BACKLOG.md` (supervisor hook as **operator-gated** line —
                  do NOT wire into `pipeline_supervisor.sh` in this W).
Forbidden writes: pipeline_supervisor.sh (P4 — operator approves hook separately).
Acceptance:       dry-run mode lists would-seal units; apply mode packs ≤N per run
                  (default 50, configurable); skips `active`/`scheduled`; log counters
                  surfaced (packed, skipped, failed).
Rollback:         revert commit.
Exit evidence:    commit hash; dry-run log; BACKLOG hook line.

### W-E6 — (Optional) mm_calibrate --event
Purpose:          per-event calibration slice for Sports tiers.
Allowed reads:    load(event=...).
Allowed writes:   `tools/mm_calibrate.py`; one test.
Forbidden writes: calibration math.
Acceptance:       runs on sealed pack; output path `work/mm/calibrate_event_<key>.json`.
Rollback:         revert commit.
Exit evidence:    commit hash; sample JSON path.

---

## 6. Validation harness (`event_validate.py`)

Checks are **blocking** for `sealed` packs unless marked REPORT-ONLY.

| ID | Check | Blocking | Description |
|---|---|---|---|
| V-EP1 | Partition conservation | yes | `pack_rows == count(warehouse filter unit, [win_start, win_end])` per table |
| V-EP2 | Cross-day completeness | yes | For units with `archive_days_spanned.len > 1`, every spanned day has ≥1 row OR documented `scheduled` |
| V-EP3 | No time leakage | yes | All rows satisfy `win_start_us <= ts_utc <= win_end_us` |
| V-EP4 | Market membership | yes | Every market in index ⊆ pack; no foreign market rows |
| V-EP5 | Bracket coverage | yes | For ME bracket events, all index markets present in dim |
| V-EP6 | Q7 exclusion | yes | No `KXMVE*` rows |
| V-EP7 | Manifest integrity | yes | File md5s match; row_counts match parquet/csv |
| V-EP8 | Idempotent rebuild | yes | Two consecutive packs → identical md5 |
| V-EP9 | LOCF reconstruct | yes | Random sample of 100 timestamps: `load(event, ffill=True)` book matches pack query |
| V-EP10 | Trade completeness | yes | No duplicate `trade_id`; trade count matches warehouse filter |
| V-EP11 | Window divergence surfaced | REPORT | If `window_divergence`, report delta hours |
| V-EP12 | Calendar-split recovery | yes | For fixture cross-midnight event: rows on day D **and** D+1 present; sum equals V-EP1 |
| V-EP13 | Lifecycle anchor | REPORT/Fork A | If `t_determined_us` set, `win_end >= t_determined`; else manifest notes `lifecycle_unavailable` |
| V-EP14 | CSV export fidelity | yes | Export row counts == pack counts; single `event_ticker` per event folder; manifest `completeness=pass` |
| V-EP15 | Interior-gap completeness (AF-1) | yes | Cross-reference `[win_start_us, win_end_us]` against the known capture-gap record (`work/quality_log.ndjson` data_loss windows + freshness/incident STALE spans + inter-record gaps in the raw feed). ANY overlap ⇒ `completeness=degraded` (NEVER `pass`), with the offending gap window(s) surfaced in `_manifest.json.gaps[]`. V-EP2's "≥1 row per spanned day" is day-granular and CANNOT see a sub-day hole (rows exist on both sides); V-EP1/V-EP10 are pack↔warehouse self-consistency and pass on holed data. This is the ONLY check that catches an interior outage (e.g. a 401/reconnect gap mid-game overlapping the settlement-convergence run — highest vol, Q6/Q8) before a green pack feeds a Q2 bound computed on holed data. |

**completeness states (manifest):** `pass` (all checks green, no gap overlap) ·
`degraded` (V-EP15 gap overlap — usable with caveats, NEVER counts as pass, gaps
listed) · `fail` (a blocking V-EP other than V-EP15 failed). A Q2/backtest
consumer MUST treat `degraded` as "holed — do not trust the bound."

**Red fixtures** (`tests/fixtures/event_pack/defects/`):

| fixture | Injected defect | Expected |
|---|---|---|
| `truncated_window` | pack built with narrow window | V-EP1 FAIL |
| `foreign_market_row` | row from other event_ticker | V-EP4 FAIL |
| `leaked_timestamp` | row at ts outside window | V-EP3 FAIL |
| `md5_mismatch` | corrupt manifest md5 | V-EP7 FAIL |
| `split_incomplete` | drop day D+1 rows | V-EP12 FAIL |
| `mve_row` | KXMVE ticker inserted | V-EP6 FAIL |
| `interior_gap` | remove all rows in a mid-window sub-day span (both sides retain rows) while a quality_log data_loss entry covers that span | V-EP2/V-EP1/V-EP10 PASS but V-EP15 FAIL ⇒ completeness=`degraded`, gap window in manifest (AF-1) |

---

## 7. Robustness tests (accuracy + failure modes)

### 7.1 Synthetic accuracy suite (`tests/test_event_pack.py`)

1. **Two-day single event:** Archive days D and D+1 with one `event_ticker`,
   overlapping times 23:00→01:00. Pack → V-EP1/2/12 PASS.
2. **Bracket event:** 4 markets, ME=true; all appear in pack; V-EP5 PASS.
3. **Head-to-head:** 2 markets, no strikes; V-EP4 PASS.
4. **Market-unit crypto:** 15-min market; pack keyed by `market_ticker`; event
   with 3 markets yields 3 packs.
5. **Catalog-only scheduled:** open_time in future, no obs → `status=scheduled`,
   no pack file, index row only.
6. **Catalog incomplete:** missing close_time → `catalog_incomplete=true`, window
   from observed only; WARN in report.
7. **Divergence:** catalog ends before last trade → merged window extends;
   V-EP11 REPORT, V-EP1 PASS.

### 7.2 Live warehouse smoke (operator-gated, not in `make check`)

```sh
python3 tools/event_index.py --refresh
python3 tools/event_measure_split.py --days 14
python3 tools/event_pack.py --unit <cross_midnight_sports_event> --force
python3 tools/event_validate.py --unit <same>
python3 tools/mm_backtest.py --event <same> --pessimistic
```

Pass criteria: V-EP1–V-EP10 + V-EP12 PASS; backtest runs without `--date`.

### 7.3 Fuzz / scale robustness (opt-in, `BENCH_EVENT_PACK=1`)

- Pack 100 random sealed Sports events from index; all V-EP1 PASS.
- Peak RSS logged; must stay < 4 GB on Mac 16 GB (no gold-scale memory).
- Runtime budget: < 30s for 100 small events (report-only).

### 7.4 Regression guards

- `make check` + `tests/run_pipeline.sh` unchanged green (no capture edits).
- `tests/test_pipeline_contract.py` still PASS.
- `warehouse.load(start=, end=)` byte-identical behavior on day queries.

---

## 8. Execution order (DAG)

```
W-E0 (measure + policy)
  → W-E1 (index, Fork B windows)
    → W-E2 (pack)
      → W-E3 (validate + load three-axis)
        → W-E4 (mm_backtest market/event/series)
          → W-E7 (CSV export)   ← operator deliverable
            → W-E5 (daily pack job + BACKLOG hook)
              → W-E6 (optional calibrate)
  W-LC (Fork A lifecycle) — parallel after W-E1, operator-gated; upgrades index accuracy
```

One W per fresh session; independent audit after every W (MASTER_SEQUENCE rule).

---

## 9. Acceptance (demonstrated, not described)

```sh
make check && tests/run_pipeline.sh

# Core event-pack contract
python3 -m pytest tests/test_event_index.py tests/test_event_pack.py \
  tests/test_event_validate.py tests/test_warehouse_event.py \
  tests/test_mm_backtest_event.py -q

# Red-fixture proof (must FAIL when defect injected)
python3 tools/event_validate.py --unit defect_truncated_window   # exit nonzero

# Live smoke (operator, cross-midnight event ticker from W-E0 report)
python3 tools/event_index.py --refresh
python3 tools/event_pack.py --unit <TICKER> --force
python3 tools/event_validate.py --unit <TICKER>
python3 tools/mm_backtest.py --event <TICKER>
```

---

## 10. Rollback

All artifacts under `work/event_packs/` are derived — delete and rebuild from
archive. No capture/archive mutation. Rollback per W = revert commit + optional
`rm -rf work/event_packs/`. `warehouse.load(event=...)` rollback = revert W-E3
only; day-mode queries unaffected.

---

## 11. Risks to production (P4)

| Component | Risk | Mitigation |
|---|---|---|
| ws_shadow / supervisor | none | not touched |
| ingest / export | none | not touched |
| DuckDB staging lock | read-only load() with retries (existing) | short queries only |
| Disk | index + packs ≈ fraction of archive | prune `data/` for `failed`; index retained |
| False window (cut off live match) | research wrong | merged window + divergence flag + V-EP1/12; never narrow below observed |
| False window (too wide) | extra rows | acceptable for research; padding in YAML |

---

## 12. §6 Self-audit (plan vs GUARDRAILS)

| # | Question | Answer |
|---|---|---|
| 1 | Named phase, no gate skip? | Phase 1.5 research; no live trading |
| 2 | Live orders? | No |
| 3 | Log-odds + fees in strategy? | Unchanged; wiring only |
| 4 | Pessimistic bound? | mm_backtest retains pessimistic path |
| 5 | WS data for trading? | N/A (research layer) |
| 6 | Tests + economic-sign? | V-EP* + backtest sign tests unchanged |
| 7 | Pipeline continuity? | Capture/ingest/export untouched |
| 8 | Reversible + bounded? | Derived packs only; per-W rollback |
| 9 | Docs updated? | warehouse_schema + RUNBOOK in W-E3/E4 |
| 10 | Green status honest? | D2: divergence + incomplete catalog surfaced |

---

## 13. Open questions for operator (before W-E0 execution)

1. **Padding defaults:** Sports pre_pad 2h / post_pad 1h — confirm or adjust.
2. **Crypto unit:** `market` vs `event` for 15-min series — confirm.
3. **Supervisor hook:** after W-E5, approve daily `event_pack_daily.py` in
   post-export window (alongside mm_research)?
4. **S3 layout (AWS STEP 1):** mirror `work/event_packs/` to
   `s3://.../product/event_packs/` — confirm prefix.

---

## 14. Context for auditors

This plan **does not** replace calendar archive. It adds a queryable, validated
**event/session layer** so `mm_backtest`, `gate_calc`, and future calibration
can address "one match, one debate, one release" without manual multi-day
stitching. The highest-value proof is **V-EP12** on real cross-midnight Sports
events identified in W-E0's split report.