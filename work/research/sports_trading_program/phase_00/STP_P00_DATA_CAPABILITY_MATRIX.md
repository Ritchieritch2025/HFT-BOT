# STP_P00_DATA_CAPABILITY_MATRIX — what exists, coverage, what can/cannot be studied (STP-P00-W01)

All numbers measured this session on the LOCAL (Mac) tree, read-only (ls/du/
wc/manifest.csv/parquet DESCRIBE/in-memory duckdb over parquet; staging.duckdb
stat-only, never opened). Production capture moved to EC2 2026-07-09; the
local tree is a frozen historical mirror — EC2-side inventory is quoted from
documented facts and marked NOT-MEASURED-HERE where no local evidence exists.

## 1. What exists locally (measured)

| layer | content | span | size / rows | provenance |
|---|---|---|---|---|
| Raw firehose `work/raw/` | ticker(L1)+trade WS envelopes, ALL markets; envelope carries `recv_mono_ns, recv_wall_ns, source, channel, source_ticker, source_sequence, sid, stream_epoch, raw` (verbatim exchange JSON incl. ts_ms, taker_side, *_dollars, count_fp) | 2026-07-06 08:18 UTC → 2026-07-09 23:09 UTC (4 date dirs) | 90G (07-06:10G/49f · 07-07:21G/93f · 07-08:28G/121f · 07-09:32G/133f) | ls/du/head of first+last files |
| Archive facts `work/warehouse/facts/` | orderbooks_l1 50,257,870 rows/266 files (7 Class-A categories) · trades 13,295,310 rows/431 files (16 categories incl. _unclassified) · orderbooks_full 103,252 rows/2 files | L1+trades 2026-07-06..08, NO gaps in span; **orderbooks_full 2026-07-06 ONLY** (Sports/Baseball 103,250 + Soccer 2) | 757M total | manifest.csv (700 rows) awk aggregation |
| Staging | staging.duckdb | frozen at 2026-07-09 ≈19:17 UTC | 2.6G | stat only |
| Catalog/dim | series 11,294 · series_classified 11,294 · events 80,000 (capped) · markets 80,000 (capped) · settlements 8,000 parquets; dim/latest CSVs (markets 80,302; events 81,616; series 11,297) + 4 daily snapshots | as of 2026-07-09 | 28M + 1.1G | duckdb counts / wc -l |
| Gold | date=2026-07-06 only (bin + loader/validate/V5/V7 reports + quarantine) | 1 day | 5.1G | ls/du |
| Event packs | index.parquet = **8 test-fixture rows, zero real packs** (all pack_path NULL); capture_gaps.csv 62 gap intervals (2026-07-06 00:00 → 07-08 ≈14:00 UTC); 4 real tennis event EXPORTS under work/event_exports/ | — | 596K | parquet rows / wc |
| S1 research | dim_segments.parquet 2,904 Tennis markets (ITF 1883/Challenger 526/ATP 332/WTA 156/_unseg 7); maker_edge_pilot checkpoints: book.parquet 4,413,144 L1 rows (07-06..08), markout/trades parquets, results.json (2026-07-10), index.html | Tennis 07-06..08 | 85M | parquet counts / RESUME.md |
| Latency baseline | work/latency_baseline/samples.ndjson 740 samples, 2026-07-09 02:45 → **2026-07-11 18:28 UTC (still accruing — the only living local feed)**; warm_p50 ≈30–35ms Mac→Kalshi | 07-09..now | 396K | wc/head/tail |
| Sports coverage (archived L1, 07-06..08) | ≈**44,137 distinct Sports markets** across 21 subcategories: Baseball 16,811 (8.19M L1 rows) · Golf 6,191 · Basketball 4,639 · Soccer 4,415 · Cycling 3,371 · **Tennis 2,904 (4.52M)** · Football 2,616 · Esports 1,240 · Motorsport 608 · MMA 390 · Chess 377 · Cricket 214 · Hockey 105 · Boxing 92 · … Trades: Soccer 2.59M · Tennis 2.04M · Baseball 1.31M rows | 3 days | — | duckdb over facts parquets; manifest |

## 2. Timestamps / decision clock

- RAW envelopes carry recv clocks from day one (2026-07-06) — recv-clock
  research is possible from raw locally.
- **Local archived parquets are PRE-TL1 schema**: DESCRIBE shows NO ladder
  columns (`exchange_ts_us/recv_wall_ns/recv_mono_ns/local_recv_ts_us`);
  `ts_utc` is exchange time on most rows ⇒ replaying it as a decision clock
  is look-ahead bias (docs/warehouse_schema.md:108-113).
- Ladder-era warehouse rows exist ONLY on EC2 (W-TL1 ingest deployed
  2026-07-10/11 — SESSION_LOG/W02 deploy status). NOT-MEASURED-HERE: EC2
  row counts/coverage; a P02 W on/against EC2 data must measure them.
- Kalshi exchange timestamps are ms-granular; sub-ms conclusions unsupported
  (warehouse_schema.md:103).

## 3. What can be studied NOW vs what CANNOT be claimed

CAN (locally, discovery/EXPLORATORY only — see contamination note):
- All-sports (and all-category control) L1/trade structure: spreads,
  arrival processes from RAW (per-channel, recv-clock), lifecycle spans,
  zero-trade/one-sided rates, cross-sport comparison — 3 archived days +
  4 raw days; the §15 atlas prototype methodology.
- Trade-conditioned markouts on exchange-clock ASOF = DIAGNOSTIC ONLY.
- DQ/gap accounting (capture_gaps + manifest reconciliation).

CANNOT be claimed from existing data (each confirmed ABSENT locally):
- **Own-order/fill/private execution data: ABSENT** (no fill channel in raw;
  no portfolio data files) ⇒ no real fill-probability, queue-position or
  cancel-effective distribution; Level-C simulator calibration impossible
  until operator-authorized probes (P10/P11).
- **Queue data: ABSENT** — L1 is change-only top-of-book (change-only rows +
  heartbeats), orderbooks_full = 1 day × ~4 watchlist markets ⇒ queue-aware
  (Level B) research not supportable today; strict-through (Level A) is the
  only executable historical fill scenario, exactly as the prompt binds.
- **Sequenced full-depth L2 history: effectively ABSENT** (no
  orderbook_delta channel in the main firehose; `config/depth_watchlist.txt`
  does not exist; ws_sid/ws_seq columns added 2026-07-07 but local
  orderbooks_full predates continuous capture). Prompt §13 DATA-4 "targeted
  L2" requires the future W06/depth-expansion waves (EC2).
- **Synchronized sports score/game-state: ABSENT** (schema slot
  `facts/game_data/` reserved, not built) ⇒ per prompt §13 DATA-6: no claim
  that a price move was caused by a sports event; scheduled-start and
  pre-match/in-play classification must come from catalog/lifecycle fields
  and must be versioned (P02 work).
- **External odds data: ABSENT** (Track C deferred; D-1 external tools all
  deferred).
- Maker fee facts: `config/kalshi_facts.yaml` fees.verified=false (OQ-1
  unratified) ⇒ fee class UNKNOWN handling fail-closed; gate-mode fee
  computation refuses (verified pattern in tools; H-36 family). UNKNOWN fee
  markets are ineligible for profitability gates (§12).

## 4. Does missing data BLOCK the primary hypothesis?

- Primary claim (pre-match market-dynamics spread capture under certified
  strict-through, one contract): **NOT blocked by missing L2/score/own-order
  data** — Level A strict-through needs lossless L1+trades with recv clocks
  and sequence evidence. BUT it IS blocked today by: (a) local archive
  lacking recv-clock columns (pre-TL1) — binding runs need EC2 ladder-era
  data, which begins ≈2026-07-10/11 ⇒ accumulation is the gating resource
  (COLLECT_MORE posture); (b) unsealed splits + prior exposure (C-6/C-11);
  (c) fee facts unratified (OQ-1) for fee-exact accounting.
- Queue-aware diagnostics (Level B) and simulator calibration (Level C):
  blocked by missing targeted L2 and own-order data respectively — later
  phases/releases (P04/P05 design can proceed; P10/P11 need live releases).
- Sports-state-dependent strategies (Track D class): blocked by DATA-6
  absence AND by authority (PROPOSAL_ONLY).

## 5. Prior-exposure inventory (seed for the P02 ledger — C-11)

Tennis 2026-07-06..08 (S1 full pipeline incl. PnL/markouts) · all-category
mm_calibrate/mm_research/mm_scan/gate_calc outputs 2026-07-06..10 ·
BADAMS cricket episode 07-06 · Muchova–Gauff export (outputs/, 07-10) ·
operator's own live fills/settlements knowledge through 2026-07-11 (D-1.1
reconciliation). None of these dates/events may enter VALIDATION or
HISTORICAL_CONFIRMATION for the sports program.

## 6. EC2-vs-local split (operational fact for every later phase)

Local newest: raw 07-09 23:09 UTC · facts 07-08 · staging 07-09 · catalog/
dim 07-09 · gold 07-06 · research derivations 07-10 · latency samples live.
No local mirror of EC2-era capture exists. EC2 production (NOT measured
here): full-market L1 for all 17 categories per rider (b) config
(`config/market_classes.yaml` read: every category class_a_full_l1,
class_b empty), W-TL1 ladder live, W06 targeted-L2 spec approved but not
landed (SESSION_LOG 2026-07-11 17:05 sidebar). P02's first data W must do
its inventory against the EC2 warehouse, not this mirror.
