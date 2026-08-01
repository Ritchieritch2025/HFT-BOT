# ALPHA-SPRINT-01 MM-POL — Politics Passive Quoting Preregistration

Date: 2026-07-25
Stratum (per 2026-07-25 audit principle): politics/elections only; no
cross-stratum parameter transfer; mixed-pool-only significance = ARTIFACT.
Cross-declaration: multiple-comparison budget of this program is closed
within this document; no reads of H6/H6b VALIDATE splits.

## Edge classification and persistence story

Liquidity provision (earning the spread). Counterparty: opinion-driven,
price-insensitive retail flow in slow politics books. Evidence base:
- H4 (aggressive taker order-flow): −2..−4c gross across all locked
  sports — the taker side of this trade loses;
- SCAN preliminary census v2 (2026-07-25): 72k sampled-minutes of gross
  complement mispricing and ~26 万 ladder inconsistencies collapse to a
  few dollars capturable after spread+fees — the tollbooth keeps the
  money; we propose to BE the tollbooth;
- politics book p95 same-side inter-update gap ≈ 55 min — slow books
  give a quoter time to reprice; stale-side event map (394k events)
  identifies where resting quotes rot (our selection pool AND our risk).

## Stage A — replay backtest (must pass before any live order)

Universe: politics+elections markets from h6b_universe_v4
(sha b2993b87…), window = latest contiguous published block.
TRAIN = first half (pool selection, quote params), VALIDATE = second
half, sealed before first VALIDATE read.

Strategy template (frozen lattice, not a search space):
- quote one or both sides at distance d ∈ {1, 2, 3} ticks inside the
  prevailing touch, in markets from the selection pool;
- pool rule (fit on TRAIN only): spread ≥ S_min ticks, same-side update
  gap p50 ≥ G_min, displayed depth at touch ≥ D_min;
- requote/cancel when mid moves ≥ 1 tick; max holding = close or
  bracket ±10c;
- fill model: STRICT — a resting quote fills only when the opposite
  side's executable price crosses our level (trade-through), never on
  touch alone; queue position assumed WORST (entire displayed size at
  our level fills first);
- costs: maker fee from live schedule (fallback frozen 2026-07-07:
  1.75% × p(1−p), ceil per order, FEE_MODELED flagged); adverse-fill
  1-tick haircut on every liquidation; carry to settlement uses
  settlements_v4 truth, missing-truth = SETTLEMENT_TRUTH_INCOMPLETE
  halt (no UNRESOLVED masking).
- baselines: (a) no-trade, (b) hold-favorite, (c) random-market same
  cadence quoting. Must beat all three net.

Primary endpoint (sealed): mean net PnL in cents per filled contract,
politics stratum, VALIDATE, reported per event-category sub-stratum;
cluster-robust by event_cluster; 10k cluster bootstrap CI; C1-style
timestamp shuffle control on fill opportunities.

Kill (Stage A): VALIDATE net/contract ≤ 0, or positive in <2 of the
top-3 event categories by fill count, or C1 shuffle EV ≥ observed.

## Stage B — $10 micro-live probe (only if Stage A passes + operator go)

Purpose hierarchy (in order): (1) first VERIFIED cash P&L through the
pnl spine — fee ratification end-to-end; (2) realized vs modeled edge
gap (fill rate, adverse-selection markout, realized fees); (3) dollars
(irrelevant at this size).

Sizing (REVISED 2026-07-25: operator raised test bankroll to $200):
- CLIP = 5 contracts (amortizes the ceil-per-order fee penalty ~5x vs
  1-lot; backtest reports both clip=1 and clip=5 economics);
- caps: ≤ $20 per market, ≤ $40 per event_cluster, ≤ $200 total
  exposure; ≥ 8 distinct clusters or don't deploy that day;
- quotes rest on the cheap side (10–40c); never cross the spread;
  cancel-on-stale (own quote age > G_min/2 with mid moved) mandatory.

Scaling ladder restated from the $200 base: B=$200 probe → C=$1,000 →
D=$5,000, with the same gates as below (thresholds unchanged; the $10
column is superseded).

Measurement (2-week probe, review daily):
- every fill reconciled: exchange statement vs spine ledger vs model,
  to the cent — a single unexplained cent is a P0 defect, halt;
- realized net edge per contract with bootstrap CI vs Stage A modeled;
- fill rate and markout at 1m/10m/1h vs STRICT-fill assumption.

Kill (Stage B): cumulative net < −$5, or any unreconciled cash, or
realized edge < (modeled − 2c) after ≥100 fills.

## Scaling ladder (pre-committed; each step needs ALL gates)

| Step | Capital | Gate to advance |
|---|---|---|
| B | $10 | ≥100 fills, reconciled to the cent, realized net edge > 0 (CI excludes 0 one-sided 90%) |
| C | $100 | 4 consecutive weeks realized net > 0; realized-vs-modeled gap < 1c; no kill triggers |
| D | $1,000 | edge stable across ≥3 event categories; capacity check: our size < 10% of touch depth at p95; operator sign-off |

No step is ever skipped; a kill at any step returns to the previous
step's capital, not to zero research.

## What this does not claim

No claim until Stage A completes on sealed splits. The $10 probe is an
instrumentation test that happens to trade; its dollars are noise by
design. VPIN remains EXPLORATORY-only per the 2026-07-25 audit sync and
is not used anywhere in this program.

Biggest hole: STRICT worst-queue fill model may be so conservative that
Stage A under-fills to n too small for the endpoint — if so the honest
outcome is NOT_ESTIMABLE and the fix is longer windows, not looser fill
assumptions.
