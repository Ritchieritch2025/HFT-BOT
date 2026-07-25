# ALPHA-SPRINT-01 H5 Preregistration — AS02-CROSS-MARKET-DEVIATION

Date: 2026-07-25
Status: `PREREGISTERED_BEFORE_IMPLEMENTATION_RUN`
Cash-PnL claim: forbidden
Arbitrage / payout-exhaustiveness claim: forbidden
Execution / fee / latency / fill claim: forbidden

This document is written and frozen BEFORE the H5 runner executes on any
VALIDATE data. The runner refuses to run unless its frozen design constants
match this document (design hash bound into the receipt).

## Question (defensive, not directional)

When one contract in a canonical multi-market event family reprices, how
quickly do sibling contracts in the same family reprice — and is that
follower reaction faster than (a) what time-reversed data shows and (b) what
unrelated-market pairings show?

The intended product is a defensive cancel/reprice warning: "a related leg
just moved; your resting quote in this family is stale-at-risk." It is NOT
an arbitrage signal, NOT a directional trade, and produces no cash-PnL
number.

## Exact inputs (strict identity — no event_proxy)

1. **Family membership**: `TABLES/MARKET_GRAPH.parquet` from the sealed
   Deep03 run `mode1-20260710-20260717-3cde714ed188-38f8763d13c0-a1`,
   SHA-256 `78d2f23bb8ffb1b2c021e5e61535a0f54e2d42fda3354cf9cd6620d5483bd278`
   (verified byte-for-byte against the sealed
   `FULLSCOPE_MARKET_GRAPH_RESULT.json` binding on 2026-07-25).
   Join strictly on `(date, market_ticker)` and keep ONLY rows with
   `mapping_status = 'CANONICAL_FAMILY_EVENT'`; the family key is
   `event_ticker`. `event_proxy` (b03_event_market_keys) is NOT used for the
   main estimand: its coalesce fallback admits self-fallback roots.
2. **Quotes**: `l2_replay` stage of checkpoint namespace
   `source-c08083fe0f6a05cb60f95f1eee70d61a8075792e83c8f72c62927254c36b1979`
   (same manifest/hash verification discipline as the H4 runner).

Known universe from these inputs (family = ≥2 captured legs on that date):
2026-07-12: 91 families / 193 legs; 2026-07-15: 122 / 274; 2026-07-17:
104 / 237.

## Split

* TRAIN: 2026-07-12 and 2026-07-15 — family admission, all thresholds,
  burst-window and any tie-breaking constants are fitted here ONLY.
* VALIDATE: 2026-07-17 — touched only after the TRAIN model seal
  (`H5_TRAIN_MODEL_SEAL.json`) is written; the runner enforces order.

## Event definitions (all constants frozen here)

**Valid quote row**: `book_valid = TRUE`, `classification = 'DELTA_APPLIED'`
(snapshot rows are anchors, not signals), `topology = 'TWO_SIDED'`,
non-NULL mid; NULL in any gating field blocks the row (BLOCK_NOT_ZERO), it
is never coerced.

**Leader event**: a valid quote row of leg L in family F at effective time t
whose mid differs from L's previous valid mid by ≥ 1 tick (100 e4 units),
with `snapshot_epoch` unchanged from the previous valid row (no
reconnect-crossing events).

**Burst de-duplication**: within a family, leader events closer than
`BURST_DEDUP_US = 1_000_000` (1s) to the previous ACCEPTED leader event of
that family are dropped (first event wins). This kills cascade
double-counting inside one news impulse.

**Simultaneity set-aside**: if two legs of one family emit a leader event at
the exact same `t_us`, the pair is recorded in an `ambiguous_simultaneous`
counter and BOTH events are excluded from the main estimand (no ordering
claim is honest at equal timestamps).

**Follower observation**: for each accepted leader event (F, L, t), every
sibling leg S ≠ L of F with a valid book at or before t contributes one
trial. Reaction = S emits a valid mid change (≥ 1 tick, same-epoch) in
(t, t + H]. Horizons H: **100ms primary, 1s secondary**, 5s sensitivity.
A sibling with no valid pre-event book is counted in `no_book_excluded`,
never silently dropped.

## Controls (both mandatory)

1. **Time-reversed (mirrored pre-event window) control**: the SAME accepted
   leader events with the observation window mirrored to [t − H, t): does
   the sibling move in the H BEFORE the leader event?  Genuine causal
   lead-lag is pre-quiet and post-busy; clustering artifacts light up both
   windows.  (Re-deriving leaders on a literally reversed stream is NOT
   discriminating: any fixed-lag echo pair is symmetric under reversal —
   the roles simply swap — so the mirrored-window form is the honest
   implementation of the reversal placebo.)
2. **Unrelated-pairing control — `DIFFERENT_EVENT_TICKER_PROXY`**: each
   accepted leader event is re-paired with pseudo-siblings drawn (seeded,
   deterministic) from a different `event_ticker` on the same date, matched
   on sibling count. The sealed MARKET_GRAPH states
   `root_mapping: NOT_ATTEMPTED`, so different `event_ticker` is NOT an
   authenticated unrelated real-world root; the receipt therefore carries
   `unrelated_root_control_authenticated: false` and this control is named
   as a proxy everywhere it is reported.

## Uncertainty (cluster-aware, no naive trigger counting)

The unit of inference is the FAMILY, not the trigger. Per-family reaction
rates are computed first; reported statistics are across-family means with a
seeded nonparametric cluster bootstrap (resample families, B = 2,000) giving
95% intervals. Raw trigger counts appear only as diagnostics.

## TRAIN-only admission (frozen into the model seal)

A family is admitted iff on EVERY TRAIN date it has ≥ 2 captured legs and
≥ `MIN_TRAIN_LEADER_EVENTS = 20` accepted leader events. The admitted family
list, all constants above, and the input bindings are sealed in
`H5_TRAIN_MODEL_SEAL.json` (content-addressed) before VALIDATE is read.
VALIDATE evaluates admitted families only; families absent from VALIDATE are
reported as attrition, not silently dropped.

## Decision rule (frozen)

H5's defensive signal SURVIVES only if, on VALIDATE 2026-07-17, for the
primary horizon (100ms):

1. admitted-family mean follower reaction rate after real leader events
   exceeds BOTH controls, with the cluster-bootstrap 95% interval of the
   (real − control) difference excluding zero, for each control separately;
2. the same inequality holds directionally at 1s (sensitivity may be noisy
   but must not invert with its own interval excluding zero).

Otherwise H5 is KILLED as a defensive signal in this revision. Either way,
no fee, fill, latency, or cash-PnL claim is made; survival only admits H5 to
the shared PnL-spine pipeline for exact-fee defensive evaluation.

## Artifacts

`H5_EVENT_STUDY_RECEIPT.json` (bindings + counters + results),
`H5_EVENT_STUDY_REPORT.md`, `H5_TRAIN_MODEL_SEAL.json` — same fail-closed,
content-addressed discipline as the H4 runner (`tools/research/alpha_sprint/
h4_orderflow_event_study.py`). Runner:
`tools/research/alpha_sprint/h5_cross_market_deviation.py`.

## Registry binding

`AS02-CROSS-MARKET-DEVIATION` in `Deepresearch V3/registry/frozen/
ALPHA_SPRINT_01_V1.json`; lineage A10 / C04 / C06 / C07. The C01/C02/C03
payout-verified trading revision is explicitly OUT of scope here.
