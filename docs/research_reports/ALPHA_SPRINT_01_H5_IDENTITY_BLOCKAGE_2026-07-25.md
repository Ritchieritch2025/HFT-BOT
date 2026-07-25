# ALPHA-SPRINT-01 H5 — Strict-Identity Universe Is Empty (BLOCKED, not killed)

Date: 2026-07-25
Status: `BLOCKED_ON_CANONICAL_DIM_COVERAGE`
Cash-PnL / arbitrage / execution claims: none (nothing was measured)

## What happened

The preregistered H5 runner (commit `8342343`, module SHA
`0b10bf2db53461c65cf8b93dabad0134cae588c3719fe56c160347eb258492a3`, prereg
`ALPHA_SPRINT_01_H5_PREREG_2026-07-25.md`) executed on W09 against the
sealed inputs and REFUSED fail-closed:

```
H5_EVENT_STUDY_REFUSED: no canonical multi-leg families with captured L2 on 2026-07-12
```

That refusal is the correct output. Under the audit's identity boundary —
family membership ONLY from the sealed MARKET_GRAPH
(`78d2f23b…`, `mapping_status = CANONICAL_FAMILY_EVENT`, joined on
`(date, market_ticker)`), `event_proxy` forbidden — the measured universe
is:

| Date | L2-captured tickers | of which CANONICAL_FAMILY_EVENT | multi-leg families |
|---|---:|---:|---:|
| 2026-07-12 (TRAIN) | 243 | **0** (241 MISSING_MARKET_DIM, 2 MISSING_EVENT_DIM) | 0 |
| 2026-07-15 (TRAIN) | 325 | 37 | 17 |
| 2026-07-17 (VALIDATE) | 295 | **0** (295 MISSING_MARKET_DIM) | 0 |

TRAIN admission requires both TRAIN dates; VALIDATE has zero canonical
legs. The strict-identity H5 estimand cannot be computed on this
checkpoint. The earlier "104 families / 237 legs on 7/17" intersection was
derived from `b03_event_market_keys` — i.e. `event_proxy`, exactly the
identity the audit excluded from the main estimand.

## Root cause

The targeted L2 watchlist (sports single-game markets) is essentially
absent from the canonical dim mapping: `dim_market_date` rows for these
tickers lack an authoritative event binding, so the graph classifies them
`MISSING_MARKET_DIM` (the same coverage gap the sealed graph result
quantifies globally: 433,822 MISSING_MARKET_DIM market-days vs 15,525
canonical). 2026-07-15's partial coverage (37 legs) shows the mapping CAN
cover watchlist markets when dim happens to include them.

## Paths forward (decision needed — audit + operator)

1. **Repair the identity layer (preferred, durable)**: extend the dim/
   catalog mapping so watchlist markets carry their `event_ticker` — the
   exchange catalog demonstrably has series→event→market for these markets
   (the L2 selector navigates it live). Then H5 runs unchanged, strict.
2. **Audit-sanctioned descriptive variant**: run the identical measurement
   on `b03_event_market_keys` families, permanently labeled
   `DESCRIPTIVE_PROXY_IDENTITY` (per the audit boundary that a descriptive
   lead/lag study remains legitimate with visible degradation). No
   promotion to the PnL spine from that variant.
3. Park H5 until ≥N sealed dates carry canonical coverage of the L2
   watchlist.

No change was made to the prereg, the runner, or the identity boundary to
manufacture a result.
