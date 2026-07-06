# Architecture Review — Current State vs Expectation (2026-07-06)

Deep review of the full codebase against the target: a profitable, low-latency,
safety-gated automated market maker on Kalshi (docs/MM_ROADMAP.md). Engine
inventory verified file-by-file by an independent read-only audit pass; data
layer verified against live runtime state.

**Verdict in one line:** the *data → research → safety-gating* half of the
system is ahead of expectation and proven against live prod; the *execution*
half is a well-tested substrate with a deliberate hole where the maker's order
lifecycle must go — the hole is phase-appropriate, but it is bigger than the
docs implied (see "Two Worlds", the central finding).

---

## 1. Target architecture (the expectation)

```
                        ┌──────────────── RESEARCH (Python, cold) ───────────────┐
                        │ warehouse (staging+archive) → scan/calibrate/backtest  │
                        │            ↓ parameters (files, reviewed)              │
Kalshi WS ─→ WS client ─→ OrderBookManager ─→ MakerStrategy ─→ QuoteManager      │
 (fills+md)   (E4, seq)    (full depth)        (log-odds AS)   (place/amend/     │
     ↑                                              ↑           cancel, queue)   │
     └── private fills channel → PositionTracker → PnL → RiskGates → KillSwitch ─┘
                                        (all C++, hot, us-east-1, sub-5ms)
```

## 2. Layer-by-layer: current vs expected

### L1 — Raw capture (LAYER 1)                                   ✅ ahead
- **Expected:** 24/7 all-market firehose, hourly logs, restart-safe.
- **Actual:** live since today: `ws_shadow` firehose (ticker+trade, all
  markets) → `work/raw/date=<D>/firehose_<HH>.ndjson`, ~1.3GB in the first
  ~1.5h, 0 errors/0 drops, launchd-supervised with single-instance lock,
  background-QoS (thermal), 3-day retention. WsRecorder rotates 256MB shards.
- **Found & fixed during this review:** ingest scanned `*.ndjson` only and
  missed rotation shards (`.ndjson.1/.2`) — staging silently fell 31 min
  behind. Glob widened, catch-up verified. Lesson encoded: every capture-side
  naming behavior needs an ingest-side test.

### L2 — Staging warehouse (LAYER 2)                             ✅ on target
- **Expected:** change-only typed rows, heartbeats, restart-safe, queryable live.
- **Actual:** `staging.duckdb` ~820k L1 + 122k trades on day one. Change-only
  + hourly heartbeat scheduler (proven by tests: 100-identical→2 rows; 3 quiet
  hours→3 heartbeats; kill/restart reconciles). Checkpointed byte offsets.
  Corrupt-input hardening earned from live fire today: timestamp
  normalization + plausibility window, ticker-shape validation (double-writer
  splice incident), lock released between cycles so readers get in.

### L3 — Archive + access (LAYER 3)                              ✅ on target
- **Expected:** write-once daily partitions, manifest+md5, one load() entry.
- **Actual:** exporter with count-verify + manifest + compression report +
  ARCHIVE_ROOT probe; `load()` routes archive/staging with no double-count;
  `--snapshot/--flat/--csv` for backtest/human exports. First real archive
  lands tonight UTC midnight. E4 types preserve sub-penny (13.4% of today's
  trades are sub-penny — UINT8 cents would have destroyed them).

### L4 — Research layer                                          ✅ ahead of schedule
- **Expected (Phase 1):** scanner + honest backtest + calibration.
- **Actual:** `mm_scan` (spread×flow, depth floors), `mm_backtest`
  (pessimistic/optimistic fill bounds — the go/no-go metric), `mm_calibrate`
  (toxicity/vol/spread per bucket, **in log-odds and price space**), all
  wired to run automatically after each midnight export. Day-one findings:
  tennis/baseball ≈ +8-9¢ edge-after-toxicity; BTC 15-min ≈ 0; naive
  touch-joining loses (pessimistic −$50) — the model roadmap (1.5) targets
  exactly that gap.

### L5 — Ops/observability                                       ✅ on target
- Dashboard (localhost, read-only, live_order refused), lifecycle gates,
  tool registry with safety classes, spec-drift watcher vs Kalshi's live
  docs (zero drift today), runbook. Weak spot: 4 duplicate dashboards + a
  stray tradingd survived a week unnoticed until today's audit — process
  hygiene is now supervised for the pipeline but not for ad-hoc launches.

### L6 — Engine: market data (C++)                               ✅ strong, but see §3
- WS client (signed, reconnect/epoch/seq-gap, firehose, ping watchdog),
  full-depth E4 `OrderBookManager` (fail-safe invalidation, checksums),
  simdjson decoder, SPSC recorder, Vyukov MPMC rings — all proven by tests
  and benchmarked (ns-scale book applies, µs-scale decode).

### L7 — Engine: execution (C++)                                 ⚠️ substrate only
- What exists and is real: signed order create (`tradingd` → HTTP/2 warm
  lanes), deterministic client_order_ids, TTL/staleness gates, strategy
  roster (fail-closed), shadow-mode decision logging, quarantine, latency
  trace probe, integer token buckets + endpoint costs + reserve-before-send
  executor (all unit-tested).
- What is missing for a maker (verified absent, not assumed): see §4.

### L8 — Safety                                                  ✅ gating / ❌ risk
- Mode gating is genuinely strong and test-proven: `data_collect` default,
  `live` requires prod+two explicit env flags, host allowlists both ways,
  bus-world execution engine throws even in Live. Zero transmits to date.
- But: **no kill switch, no position/loss caps, no reconcile action** —
  exactly the Phase-3 gate list; expected-missing, now precisely scoped.

### L9 — Deployment/latency                                      ❌ behind target (by plan)
- Current: single Mac, warm RTT to Kalshi p50 36.3ms (measured today),
  research and capture co-resident with the (future) trading path.
- Expected for live: us-east-1 box (sub-5ms), engine isolated from research,
  cancel-on-disconnect armed. CMakeLists (Linux build) kept for this.

## 3. Central finding: the engine is two disconnected worlds

**World A (transmits, shallow):** `tradingd` → REST *poll* → top-of-book in
integer cents (`wire::MarketEvent`, 88B) → `IStrategy` → single-shot order
create. No cancel, no fills, no positions, not post-only, bypasses the
reserve-before-send executor.

**World B (rich, never transmits):** WS client → full-depth E4 book →
recovery ladder → `NormalizedEvent` bus → execution engine that *refuses* to
transmit by design. This is what `ws_shadow` runs 24/7 today.

The docs' "hot path" story implicitly assumed these were one system. They are
not. **The Phase-2 engineering task is therefore precisely: drive execution
from World B** — wire the WS book into the strategy dispatch, and build the
missing order-lifecycle layer around the existing transmit plumbing. This is
a merge, not a rewrite: every primitive needed exists and is tested on one
side or the other.

## 4. Gap register (execution) — the Phase 2/3 build list

| # | Gap (verified absent) | Roadmap phase |
|---|---|---|
| 1 | Private WS fills/order-status channel (real-time fill feedback) | 2 |
| 2 | Position + PnL tracker (fills/positions are unparsed raw JSON) | 2 |
| 3 | Resting-order registry (order_id never captured → can't cancel/reprice) | 2 |
| 4 | Per-market QuoteManager (desired-vs-live quotes, queue awareness) | 2 |
| 5 | Order amend/decrease + batch create/cancel + cancel-all | 2 |
| 6 | post-only on the live path (builder supports it; tradingd doesn't pass it) | 2 |
| 7 | WS book → strategy dispatch (replace REST poll for trading) | 2 |
| 8 | Order path through reserve-before-send executor (currently bypassed) | 2 |
| 9 | Kill switch (rule-triggered + manual) + cancel-on-disconnect | 3 |
| 10 | Risk gates: position/notional/day-loss caps, per-market exposure | 3 |
| 11 | Reconcile as an action (query by client_order_id on ambiguity) | 3 |
| 12 | Typed balance/positions/fills/settlements endpoints | 2/3 |
| 13 | Maker fee awareness (some series charge maker fees) + taker fee model | 1.5/2 |
| 14 | Strategy inputs: depth + log-odds features in MarketEvent (cents-only today) | 2 |

Adopted from external audit (rodlaf post-mortem): deselect-drain lifecycle,
economic-sign unit tests, fail-closed risk snapshots, standalone panic CLI,
MVE defense-in-depth filters, Retry-After-aware degradation.

## 5. Technical debt & risk register

1. **Python 3.9 system interpreter** for the whole data layer — works, but
   pinned to an EOL runtime; migrate on the us-east-1 box.
2. **DuckDB single-writer** — mitigated (lock windows + retries + export
   pause), but the design leans on cooperative timing; acceptable for
   research cadence, never acceptable on the trading path (and isn't on it).
3. **Corruption window 08:18–08:35 UTC today** (double-writer incident):
   purged from staging; raw shards retain spliced lines (harmless — ingester
   validates). Root cause fixed (single-instance lock).
4. **orderbooks_full** covers watchlist runs only (firehose is ticker+trade);
   fine for L1-driven MM, revisit if depth research needs widen.
5. **Dashboard/ops processes** are unsupervised when launched ad-hoc.
6. **Docs drift** happened today (schema doc lagged the build twice) — the
   review habit (this doc) is the countermeasure.

## 6. Score vs expectation

| Layer | Expected by now | Actual | Δ |
|---|---|---|---|
| Data capture | prototype | 24/7 hardened, live-proven | **ahead** |
| Warehouse | design | built, tested, live | **ahead** |
| Research | none (Phase 1 was "this week") | 3 tools live + day-one findings | **ahead** |
| Ops/safety gating | partial | proven, zero transmits ever | on target |
| Engine MD | built | built + benchmarked | on target |
| Execution loop | not started (Phase 2) | substrate only, gaps scoped | on plan |
| Risk controls | not started (Phase 3) | absent, precisely listed | on plan |
| Latency/deploy | not started (Phase 4) | measured baseline (36ms) | on plan |

**Net:** no layer is behind plan; the plan itself is now grounded in a
verified inventory instead of assumptions. The one strategic correction from
this review: Phase 2 must begin with the **World A/B merge** (WS-driven
execution) before any maker logic, or the maker would be built on the polled
cents feed and inherit every failure mode we documented in the rodlaf audit.
