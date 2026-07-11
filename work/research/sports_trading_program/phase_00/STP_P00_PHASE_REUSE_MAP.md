# STP_P00_PHASE_REUSE_MAP — per STP phase: existing components, extensions, prohibitions, tests, engineering dependencies (STP-P00-W01)

Classifications reference `STP_P00_CODE_REUSE_MATRIX.{md,json}`. Every phase
below is UNAUTHORIZED until its own operator release; this map plans reuse,
it grants nothing.

## STP-P01 — Canonicalization (paper only)
- Existing: DECISIONS ledger (append-only), STATE (created by this W01),
  prompt §30 pinning targets inspected (PLAN_MM_TEST_PROGRAM,
  PLAN_RESEARCH_CYCLE_1 — section IDs + roles listed in AUTHORITY_MAP §4).
- Build: `docs/PLAN_SPORTS_TRADING_MASTER.md`; GUARDRAILS treatment table
  (§10.1 proposal → operator per-item); CLAUDE.md stale-pointer fix (C-3);
  STP-phase↔engineering-W map.
- Prohibited: research execution; any DECISIONS rewrite; prompt byte changes.
- Tests: none (docs); `git diff --check`-class only.
- Dependencies: D-2 (DONE); AUD01 PASS + operator release.

## STP-P02 — Raw data truth & split sealing
- Reuse: warehouse.load (REUSE_AS_IS) · gold chain (EXTEND) · event
  packaging/DQ + capture_gaps (EXTEND) · dim_snapshot/catalog outputs
  (PRESERVE, read-only) · sid_stream sequence truth (EXTEND).
- Build: prior-exposure ledger + hash-sealed four-way split manifest
  (§20.1; seed inventory in DATA_CAPABILITY §5); all-Sports inventory ON
  EC2 ladder-era data; dedup/timestamps/gap audits; root-event
  linkage/lifecycle/fee classes (§12).
- Prohibited: production changes (ingest/export/supervisor PRESERVE);
  build_segments as-is for the all-Sports universe (H-28); outcome-linked
  analysis before sealing.
- Tests: schema/DQ suites in-change (§28); registry entries for new tools.
- Dependencies: EC2 data access pattern decision (C-12); OQ-1 for fee-exact
  fields; C-6 split-design ruling.

## STP-P03 — Arrival/burst/reaction atlas (TRAIN only)
- Reuse: raw envelopes + storage/ReplaySource (REUSE_AS_IS) · jitter_report
  (EXTEND) · ws_recorder/capture_gaps evidence (EXTEND) · S4/B1 corrected
  methodology (H-43) · research reporting scaffolding + vendored ECharts
  (REUSE_AS_IS).
- Build: per-channel inter-arrival + rolling-window burst atlas (§15
  grids), immediate executable markouts, time-to-start regimes, all-category
  controls.
- Prohibited: markout-row spacing as book arrivals (H-43); conflated-feed
  queue claims (§22.18).
- Tests: fixture-driven percentile/burst goldens.
- Dependencies: P02 sealed splits (TRAIN only).

## STP-P04 — Maker actions & latency
- Reuse: bench_orderbook/bench_ws_decode (REUSE_AS_IS) ·
  full_chain_latency_probe (REUSE_AS_IS) · saved vendor spec + kalshi_facts
  (SAVED_SPEC_VERIFIED ceiling) · S2/S3/S5 procedures (RC1, pinned at P01) ·
  tests/analyze_full_chain_latency.py (OPERATOR_TBD registration).
- Build: §17 action matrix with implementation states; local decode/book/
  decision measurements; authorized read-only RTT (release required —
  bench_rtt EXTEND); cancel competing-risk design (§16).
- Prohibited: order probes (C5/I2 class); PLACEHOLDER-backed gates (H-18);
  memory-based API claims (E4 discipline).
- Dependencies: network_read release for authenticated measurement.

## STP-P05 — Governed fill simulator
- Reuse: replay/bus/orderbook/storage (REUSE_AS_IS/EXTEND) — deterministic
  replay substrate; fixedpoint (REUSE_AS_IS).
- Build: lifecycle/invariant spec (§23), certified strict-through engine
  (§22), exact cash/position/fee ledger (§18 pathwise identity, fee facts
  gated), terminal-inventory policy (§19), diagnostic queue model.
- Prohibited: mm_backtest fill semantics reuse (DIAGNOSTIC_ONLY, H-15..23);
  silently changing its historical semantics (§26.3); float money.
- Tests: §23 invariants incl. red-first mutation proofs; deterministic
  replay equality.
- Dependencies: P02 data truth; P04 measured latency (else CONSERVATIVE_BOUND).

## STP-P06 — Cross-Sports candidate selection
- Reuse: dim_segments concept via generalized segmentation (EXTEND of
  build_segments, H-28 fixed) · coverage_audit (EXTEND) · mm_scan/
  mm_calibrate/gate_calc as feature baselines (DIAGNOSTIC_ONLY) · pricing
  lo/fair/quote (OPERATOR_TBD — adopt only if semantics match the frozen
  market-dynamics policy) · FA-1/F2c/F2d admission doctrine
  (PLAN_MM_TEST_PROGRAM, supporting detail for §25).
- Build: hash-sealed candidate registry (§20.5), power/commercial model
  (§20.4/§20.7), nested chronological TRAIN selection, one VALIDATION
  opening.
- Prohibited: cross-layer merges; test peeking; unregistered candidates.
- Dependencies: P05 simulator; sealed splits; provisional gates (C-7).

## STP-P07 — Historical confirmation
- Reuse: maker-edge orchestration/reporting AFTER component split
  (DIAGNOSTIC_ONLY today; orchestration parts re-classified in their own W)
  · governed simulator output (P05) · vendored ECharts.
- Build: unblinding gate, one frozen strict-through run, §20.3
  calendar-day-block studentized bootstrap engine (`ci95_lower`), one-contract
  capacity report.
- Prohibited: per-match-bootstrap conclusions (C-8); extending an
  inconclusive holdout (§20.6); any deployment language beyond
  PAPER_TRADE_ELIGIBLE / SHADOW_ELIGIBLE.

## STP-P08 — Execution safety integration (engineering, subordinate to MASTER_SEQUENCE)
- Reuse: tradingd lane + gateway engine (EXTEND — convergence targets) ·
  request_executor (REUSE_AS_IS, single submitter) · risk_ledger +
  rule_engine (REUSE_AS_IS, currently UNWIRED H-14) · reconcile/account_view
  (REUSE_AS_IS) · panic (EXTEND) · ws_client/recovery/orderbook (EXTEND) ·
  wire.hpp (EXTEND — must force post_only, H-12).
- Build: one SportsDecisionCore; sequenced WS main feed (closes H-10);
  single gated OrderSubmitter (closes H-11); private state/reconciliation;
  kill/recovery/failure injection; D-1.1 four guardrails (attribution
  isolation, live-balance reserve, same-market mutual exclusion auto-pause,
  honest risk boundary).
- Prohibited: a third live transmission path (§26.4); orders of any kind;
  reordering engineering Ws vs MASTER_SEQUENCE (World A/B merge alignment).
- Dependencies: MASTER_SEQUENCE World A/B merge W mapping (C-5).

## STP-P09 — Prospective shadow
- Reuse: ws_shadow (EXTEND) · same decision core as P08 · replay parity
  substrate · provisional 5-of-7/clean-day gates (C-7).
- Prohibited: real-fill claims from shadow; any transmit (SH-2 zero-order
  proof pattern).

## STP-P10 — Micro-live calibration (requires live_order_permission=true release)
- Reuse: full S1–S6 preflight stack; panic (kill path) + panic_live
  (remains console-forbidden); account_view --assert-zero-resting;
  frozen sampling design doctrine (§24); D-1.1 guardrails restated in every
  release.
- Prohibited today: EVERYTHING here — bench_order/fill_test/preflight
  --order/live_e2e stay DO_NOT_USE.

## STP-P11 — Calibrated simulator audit
- Reuse: P05 simulator + P10 own-order data; disjoint validation doctrine
  (§24). Level-C authority change requires operator approval (§21).

## STP-P12 — Prospective profitability cohort
- Reuse: frozen decision core, actual pathwise ledger (§18), §20 statistics.
- Dependencies: new live release + adequate power; only phase that can
  support PROSPECTIVE_PROFITABILITY_SUPPORTED / LIMITED_DEPLOYMENT_ELIGIBLE.

## STP-P13 — Limited deployment & controlled scale
- Reuse: monitoring family (EXTEND: freshness/daily_check/warehouse_status/
  feed_readiness/verify_*/lifecycle_check) · FA-1 decay contract · alert
  path (reconcile alarm forwarding BACKLOG must be closed by then).
- Prohibited: automatic scaling; production pipeline coupling (separately
  protected forever).

## Cross-phase engineering-W dependencies (for the P01 mapping task)
1. World A/B merge (MASTER_SEQUENCE queue) ↔ STP-P08 — must be mapped 1:1.
2. W06 targeted-L2 rollout (EC2, approved spec) ↔ P02/P03 DATA-4 needs.
3. OQ-1 fee ratification ↔ every fee-exact computation from P02 on.
4. live_e2e register-or-retire + preflight --order mode split (C-13) ↔ any
   P08 census-clean gate.
5. S2/S3 latency measurements ↔ P04; W-K6 rehearsal ↔ P10 entry.
