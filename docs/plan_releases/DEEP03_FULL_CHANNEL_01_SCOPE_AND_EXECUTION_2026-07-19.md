# DEEP03-FULL-CHANNEL-01 — full-row exploratory execution contract

Date: `2026-07-19`

Status: `IMPLEMENTATION_DRAFT / NO EXECUTION AUTHORITY / NO AWS AUTHORITY`

## 1. Operator objective

Use every legally readable row that passes its channel-specific integrity gate
from L1, trades, targeted L2, market/catalog metadata, and fresh RFQ. Produce
one evidence-indexed exploratory report with charts, tables, coverage,
exclusions, blocked cells, and ranked monetization hypotheses.

"Full" in this release means **full rows within the exact available channel
coverage**. It does not mean that a channel existed for every date or market,
and it does not mean that every later Deep03 execution/validation phase has
already run.

## 2. Exact cohorts must remain separate

### 2.1 Base cohort

- dates: `2026-07-10` through `2026-07-17`;
- exact base releases: the eight already bound V3 reference releases;
- L1, trades, dated dimensions and catalog/market metadata: all eight dates;
- targeted full-depth L2: only dates and markets actually present and
  sequence-valid; current evidence shows L2 on `2026-07-12` through
  `2026-07-17`, and no L2 table on `2026-07-10` or `2026-07-11`;
- evidence tier: `SEALED_DEGRADED_EVIDENCE`, exploratory only.

The absence of L2 on a date or market is an explicit `ABSENT/BLOCKED_DATA`
coverage row. It is never filled, inferred, or silently removed from a
denominator.

### 2.2 Fresh RFQ cohort

- lane: `W-RFQ-FRESH-01` only;
- generation-specific strict T0; the original generation was bound to
  `2026-07-19T00:00:00Z`, and every replacement generation must carry a new
  precommitted authority, runtime commit and T0;
- old 284-object RFQ lineage and every repair derivative remain
  `DATA_INTEGRITY_BLOCKED / NO_REPAIR`;
- an RFQ date is eligible only after 24/24 analysis hours, D+1 00/01 watermark
  hours, exact seal equality, exact VersionId/SHA evidence, eligibility tags,
  and the W09 exact-version canary all pass;
- the first fresh RFQ date is bound as an independent overlay to the exact
  same-date base release. It is not inserted into the 7/10-7/17 base cohort.

The observed `2026-07-19` closed-hour receipts are not eligible: every checked
hour is `EVENT_COMPLETENESS_UNPROVEN`. Therefore 7/19 is permanently
disqualified for strict fresh-RFQ research. No parser correction may relabel,
repair or replace those source-hour receipts. The normal next candidate is the
first later UTC day captured under an independently audited replacement
generation with 24/24 clean hours; the planned candidate is `2026-07-20`.

## 3. Analytical modules

### 3.1 Base L1/trade module

Retain the audited bounded `.08` B01-B04 estimands and checkpoints:

- B01 trade-side markout surface;
- B02 one-sided duration/incidence with censoring;
- B03 linked-market estimability and residual diagnostics;
- B04 activity rhythm with active-minute denominators.

### 3.2 Market and coverage graph

Use series, event, market, catalog and dated metadata to emit:

- exact series -> event -> market membership;
- sport, market structure, scheduled phase and lifecycle coverage;
- root/family mappings with provenance and mapping ambiguity;
- zero-support, unknown-orientation and non-exhaustive-payoff cells;
- L1/trade/L2/RFQ support on every emitted cell.

Heuristic roots may be reported as `PROVISIONAL`; they cannot support an
arbitrage or payout-exhaustiveness claim.

### 3.3 L2/SNBD module

For every captured, sequence-valid L2 epoch:

- deterministic snapshot/delta replay and conservation;
- spread, depth, imbalance, microprice and two-/one-sided uptime;
- retreat, depletion, refill/rebuild and liquidity-path episodes;
- matched quiet controls, future/shift/reset controls and concentration;
- explicit gap, reconnect, snapshot reset, negative depth and right-censor
  exclusion receipts.

Processing is bounded by exact date and full market hash partitions; reducers
must preserve exact global quantiles and distinct sets.

### 3.4 Fresh RFQ module

Only after an eligible fresh overlay exists, execute:

- D01 RFQ flow census;
- D02 size/intent distributions;
- D03 lifecycle survival and right censoring;
- D04 combo/single and leg-pressure structure;
- D05 requester-hash concentration;
- D06 observable direction/volatility proxies;
- D07 RFQ-to-CLOB L1/L2 response and adverse-selection proxies.

D08 direct-quote PnL remains `BLOCKED_PRIVATE_DATA`: the public communications
feed does not prove our quote, accepted price, fill, fees or terminal cashflow.
Gross profit, net profit and fill rate must remain `null` unless separately
audited private quote/fill data are later supplied.

## 4. Accuracy and fail-closed gates

1. Every source is bound by release ID, key, exact VersionId, bytes and SHA-256.
2. Per-channel and per-partition row conservation is mandatory.
3. Every exclusion has a named count and reason; unknown orientation is never
   coerced into a valid side.
4. L2 admits only sequence-valid epochs and resets state at snapshot/epoch
   boundaries.
5. RFQ identity and lifecycle dedup are global by full RFQ ID before any
   market/date aggregation.
6. RFQ-to-CLOB joins are past-only and clock-bounded; unmapped RFQs remain in
   the coverage ledger.
7. Bounded and reference engines must agree on adversarial fixtures and a
   deterministic real subset before a result can be promoted beyond
   exploratory.
8. Any OOM, timeout, count drift, gap, ambiguity, corrupt checkpoint or input
   mutation produces no `RUN_COMPLETE` and no completed-research claim.

## 5. Resource contract

- fixed host: W09 `i-0e53d134dceffe166`, initially `r8g.2xlarge`;
- sequential bounded partitions first; parallelism only after measured
  headroom;
- do not reuse the old global RFQ stage or its damaged cache;
- before full-channel execution, either prove a strict measured disk envelope
  below current free space or expand the W09 volume. Planning target is at
  least `500 GiB` because the base checkpoint gate, L2 partitions, fresh RFQ
  source/cache and report artifacts must coexist with safety reserve;
- checkpoint payloads are content-bound and reusable only after complete
  receipt verification.

## 6. Execution order

1. Repair and independently test the fresh RFQ capture receipt/continuity
   defect without touching the primary L1/L2 collector.
2. Build and test the bounded L2, market-graph and fresh-RFQ modules.
3. Stage the base cohort on W09 while the next RFQ candidate day accrues.
4. Produce and verify the first eligible fresh RFQ overlay.
5. Run RFQ D01-D07 against its same-date base data.
6. Reduce both cohorts into one report while keeping dates, evidence tiers and
   denominators visibly separate.
7. Require independent recomputation, immutable receipts and one atomic
   `RUN_COMPLETE` before delivery.

## 7. Required delivery

- self-contained HTML report with charts and tables;
- machine-readable result tables;
- full coverage/exclusion/blocked-cell ledger;
- market/family graph atlas;
- L2/SNBD atlas;
- fresh RFQ atlas and CLOB response study;
- ranked monetization hypotheses with evidence strength and missing
  dependencies;
- exact input, checkpoint, resource, method and completion receipts.

No strategy is called profitable merely because it ranks highly. Fee/fill/
exit simulation, TRAIN, validation and confirmation remain later gates under
the adopted Deep03 plan.
