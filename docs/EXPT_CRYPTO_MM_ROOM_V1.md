# EXPT-CRYPTO-MM-ROOM v1 — Is there room for us to make markets in short-term BTC/ETH?

**Status:** DESIGN — not yet registered / not yet run
**Date:** 2026-07-24
**Line:** RC (own research), candidate for registry freeze after principal review
**Prime question (falsifiable):** Does there exist at least one cell of
(series × time-to-expiry × moneyness) in Kalshi short-term crypto markets where a
passive market maker earns **positive expected P&L per contract after exact fees**,
under a **pessimistic fill model**, with n ≫ 10k and t-stat ≥ 3?

If no such cell exists → sector is dead for MM, we spend nothing more on it.

---

## 0. Universe (fixed before any measurement)

From the 16-day tape (2026-07-07 → 07-23, `sandbox/expt_bo2026/expt.duckdb`):

| Series | Structure | Trades/day | 16d notional |
|---|---|---|---|
| KXBTC15M | 15-min up/down binary | 1.70M | $927M |
| KXBTCD | hourly range ladder (~30 strikes) | 385k | $315M |
| KXETH15M | 15-min up/down binary | 214k | $50M |
| KXBTC | hourly threshold ladder | 19k | $5.6M |
| KXETHD | hourly range ladder | 15k | $7.0M |
| KXETH | hourly threshold ladder | 2k | $0.6M |

Primary: **KXBTC15M, KXBTCD, KXETH15M**. Others measured but not decision-driving.
Per the market-structure stratification ruling: **all conclusions per series;
no pooled main conclusions.** Pooled estimation within one structure allowed only
with a deviation stamp.

## P&L identity being tested

For a resting quote that gets filled and later flattened/settled:

```
edge/contract = S/2            (half-spread at fill)
              − M(τ)           (adverse-selection markout at horizon τ)
              − F_maker        (maker fee, exact per-series)
              − C_flat         (expected cost of shedding inventory:
                                spread crossed + taker fee, amortized)
```

Every experiment below estimates one term or the joint distribution.

---

## E0 — Ground rules & instrument facts (½ day, blocking)

**Established 2026-07-24 (official sources):**
- Taker fee = `ceil_to_cent(0.07 × C × P × (1−P))` per trade (official PDF,
  eff. 2026-02-05; standard row of live fee page confirms $0.07 / multiplier 1).
  At P=50¢ this is **1.75¢/contract** — larger than the 1¢ spread. No settlement fee.
- Maker fee (where applicable) = `ceil_to_cent(0.0175 × C × P × (1−P))` = 25% of
  taker; rounding excess reimbursed monthly if >$10.
- **RESOLVED 2026-07-25 (official July 7, 2026 fee schedule PDF, independently
  re-verified from the source document):** maker fee formula is
  `ceil_centicent(M_maker × 0.0175 × C × P(1−P))` with **M_maker default 0** —
  maker fees apply ONLY to the ~85 series named in the Non-Standard table
  (sports GAME/MATCH lines, awards, etc.). **None of our six crypto series
  appears anywhere in the document → maker fee = 0, taker M = 1.** Also new in
  the July version: rounding is to the **centicent** (0.01¢) including position
  cost, not to the next cent — the old per-trade rounding tax is gone.
  Zero-both-ways series exist (e.g. KXLAYOFFSYINFO listed as `0 0`).
  Source: `/Applications/Research Ritch/kalshi-fee-schedule July'.pdf`.
- Settlement (15M/hourly): CF Benchmarks RTI; final value = **average of 60 RTI
  prices over the last minute** before expiration. This TWAP tail changes pin
  dynamics: last-minute quotes must price a partially-realized average, not spot.
- Document per series: strike definition, tick size, min order, halt rules.
- Validate `expt.duckdb` price units against the known price-convention pitfall
  (dashboard_v2 lesson): cross-check 50 random trades against raw firehose lines.
- **G0 gate (inherited #1 blocker):** fee formula ratified against real fills
  (372 fills / $954 aggregate already on file — reconcile at least the crypto
  subset, or fetch the official schedule and verify against ≥10 live fills).
  Nothing downstream is decision-grade until G0 passes.

## T0 — Timescale census: how long do opportunities actually live? (blocking for E2 granularity)

Principal directive 2026-07-24: before trusting any spread/edge number, establish
the time resolution of both the market and our measurement instrument. An edge
that lives 100ms is worthless to a stack that needs 800ms to react — regardless
of what second-scale averages say.

Measure, per series:
1. **Quote lifetime distribution** — how long the touch price survives (target
   resolution: ms). 2. **Requote latency** — after a trade or an anchor move, how
   fast the book refreshes. 3. **Kalshi-follows-Coinbase lag** — distribution of
   the delay between a Coinbase mid move and the Kalshi quote adjusting (this IS
   the lifetime of anchor-based edge; needs H3 + Kalshi capture on synced clocks,
   recv_mono_ns discipline per W-TL1). 4. **Our own reaction budget** — feed-in →
   decision → cancel/replace ack round trip measured from EC2 against the demo
   env (order_latency_test from PLAN_LIVE_VALIDATION is still an open item).

**Instrument finding (2026-07-24, from tape):** the `ticker`-channel L1 capture is
~1 Hz-conflated — median inter-update gap for KXBTC15M is 1002ms with clustering
at 1s multiples, and the "median touch lifetime ≈ 1030ms" is therefore the sampling
shutter, not the market. **Sub-second dynamics are invisible in all data captured
so far.** Consequences:
- E2 on the existing tape is valid only for horizons ≥ ~2s and to-settlement;
  sub-second markout columns (100/250/500ms) require the L2 delta capture
  (event-granular) pointed at crypto — prerequisite, not nice-to-have.
- Root cause (exchange-side conflation vs our pipeline) to be verified against a
  raw `orderbook_delta` capture with recv_mono_ns.
- **Cell admission gains a timescale test:** a cell only counts as "room for us"
  if its measured edge half-life exceeds our measured reaction budget by ≥5×.
- **Conditioning requirement (principal, 2026-07-25): quote lifetime is a mixture
  distribution — never report unconditional percentiles.** All five clocks must be
  bucketed by (time-to-expiry × volatility state). Vol state proxy pre-H3: trailing
  60s mid-change count/magnitude on Kalshi; post-H3: realized BRTI/spot vol.
  Strategy shape upgrades to double gating: time gate (exit mid zone T−5m) + vol
  gate (pull quotes when spot momentum exceeds threshold; dead-calm cells also
  suspect — no flow, no fills). Playable cell = lifetime p10 ≥ 5× our loop AND
  positive taker flow AND tolerable toxicity, per (TTE × vol) bucket. Mirrors the
  VAR σ lesson: σ1–2 moderate-vol cells were the only survivors there too.

### T0b — Anchor feasibility & BRTI basis (folded into T0/E3)

Facts established 2026-07-24:
- **Coinbase capture: proven.** H3 smoke (60s, receipts on file): public WS, no
  auth, `level2` + `market_trades` + heartbeats for BTC-USD/ETH-USD; 0 sequence
  gaps / 0 parse errors / 0 reconnects; every record dual-stamped wall_ns +
  monotonic_ns. Rate ≈ 232 KB/s → ~20 GB/day raw for 2 products (zstd ~10×;
  disk plan via B2/B3 discipline).
- **Settlement is NOT Coinbase.** Exact rule (live market JSON): resolves YES iff
  60s simple average of **CF Benchmarks BRTI** before expiry ≥ the 60s average
  before the previous 15-min mark — i.e., TWAP-vs-TWAP on a 1 Hz multi-exchange
  order-book-derived index. Licensed product; no cheap real-time feed assumed.
- **RESOLVED 2026-07-24: Kalshi streams the settlement index itself.**
  Authenticated WS channel `cfbenchmarks_value` (docs.kalshi.com/websockets/
  cfbenchmarks-value): subscribe with `index_ids: ["BRTI","ETHUSD_RTI"]` (or
  "all"); ~1 Hz ticks; each message carries the raw CF frame plus
  `avg_60s_data` (rolling 60s average — the settlement quantity, precomputed)
  and, in the final minute before each quarter-hour close,
  `last_60s_windowed_average_15min` (the exact settlement window). `seq` for
  gap detection, `received_at` upstream timestamp.
  → The capture plan gains a third leg: Kalshi `cfbenchmarks_value` alongside
  H3 Coinbase and Kalshi market data, all recv_mono_ns-stamped. Coinbase
  remains valuable as the *faster* (event-time) leading signal; BRTI channel is
  the settlement-grade truth at 1 Hz. Basis study reduces to
  "Coinbase-lead vs BRTI-tick" lag/gap measurement (still T0 material: how much
  does Coinbase lead the 1 Hz index tick, and by how much do the fast players
  see the next tick coming).
  Note: channel is auth-required → capture uses our API key; scope stamp needed
  to extend H3 authority beyond COINBASE_ONLY public-data-only.
- Model implication: near expiry the target is a partially-realized 60s TWAP —
  uncertainty shrinks deterministically inside the final minute; both strike and
  settle are averages, so the state variable is (realized-so-far TWAP, spot,
  time-left), all computable from a 1 Hz index proxy.

## E1 — Gross pool: what is being paid to makers today? (1 day, tape only)

For each series, each day: assume the incumbent makers capture the touch on every
taker trade. Gross pool = Σ over trades of (spread_at_trade/2 × qty).
Spread at trade time from L1 (S3 releases for 07-10→07-22; local staging for 07-07→09).

Output: $/day maker revenue pool per series. This is the ceiling on everyone's
gross, before adverse selection and fees. If pool × plausible-share (≤20%) − fees
can't clear our infra cost of running the line, stop here.

## E2 — Adverse selection: the markout room-map (2–3 days, tape only) ★core★

For every taker trade: markout M(τ) = sign(taker) × (mid(t+τ) − trade_px),
τ ∈ {1s, 5s, 30s, 2m, to-settlement}. To-settlement uses the settlements table
(labels for 205k markets) and captures all vol/pin risk.

Maker edge per contract at touch = S/2 − M(τ) − F_maker.

Stratify each series by:
- **time-to-expiry** buckets: >10m / 10–5m / 5–2m / 2–0m (15M); hour analog for ladders
- **moneyness**: |price − 50¢| buckets for 15M; distance-to-money strike rank for ladders
- **trade size**: ≤10 / 11–100 / >100 contracts (retail vs possibly-informed proxy)

Cell admission rule: n ≥ 10k and t-test on mean edge (per shop rule: n ≫ 10k
before believing any cell).

Output: heatmaps of after-fee maker edge per contract per cell, per series.
**Kill K1:** if edge ≤ 0 in every cell of every primary series even at the touch
under optimistic assumptions → sector dead, write kill memo, stop.

## E3 — Toxicity decomposition: who kills makers here? (needs anchor; 1 day compute)

Hypothesis: the toxic flow is spot-followers — takers who hit Kalshi quotes within
~1–2s after a Coinbase move that the quote hasn't absorbed yet. Retail noise flow
is the profitable remainder.

- **Requires the H3 Coinbase capture deployed** (BTC-USD/ETH-USD trades + level2;
  code built & smoke-tested 07-24, currently idle). Deploy → accumulate ≥5 days
  synchronized with our Kalshi capture.
- Classify each Kalshi taker trade: "spot-following" if sign matches Coinbase
  mid-move over preceding 2s exceeding a threshold; else "noise".
- Re-draw E2 heatmaps separately for the two flow classes.

Output: fraction of flow that is toxic, and the counterfactual maker edge **if we
could avoid the spot-following flow** (= the edge available to a maker with a live
anchor that pulls/repricing quotes on spot moves). This is the design spec for the
pricing model: how fast and how far quotes must move on the anchor.
Retrospective portion of E2 runs without this; E3 sharpens it on fresh data.

## E4 — Fill reality: can we actually get the passive fills? (2 days, replay harness)

Spread capture at the touch assumes we are at the front of a 1¢-wide queue with
225–325 contracts ahead. Two fill models on the two-stage replay harness:

- **Optimistic (upper bound):** filled whenever a taker trade occurs at our price.
- **Pessimistic (lower bound, the go/no-go per MM-roadmap doctrine):** filled only
  when price trades *through* our level, i.e. we are last in queue.
- Queue proxy from L1 qty at touch (we lack crypto L2 — stamped deviation;
  refine when crypto enters the L2 quota).

Simulate a naive symmetric quoter (join touch both sides, fixed size, flatten at
T−60s) per series. Output: fills/day, gross, net after fees under both bounds.
**Kill K2:** pessimistic-bound net < 0 in all primary series *and* optimistic-bound
net < our infra cost → dead.

## E5 — Inventory & pin risk (1 day, piggybacks on E4 sim)

Binaries settle 0/100. From E4's fill stream: distribution of net inventory at
T−60s, P&L variance of holding vs cost of flattening (spread + taker fee).
Test simple shedding rules: quote-skew proportional to inventory; hard flatten at
T−X for X ∈ {30s, 60s, 120s}. Output: flattening cost C_flat per contract to plug
into the edge identity; tail-loss table (worst 1% event P&L).

## E6 — Anchor-informed quoting uplift (after E3; 2 days)

The pricing-model prototype, minimal version:
- 15M binaries: P(up) from distance of Coinbase spot to strike + realized vol over
  remaining seconds (Brownian approx first; no ML).
- Re-run E4 sim with quotes centered on model price instead of market mid, and a
  pull rule on anchor moves (from E3 thresholds).
Output: net P&L delta vs naive quoter. This is the first read on whether *our own
pricing model* adds room beyond generic queue-sitting — the actual angle.

## E7 — Live shadow (promotion gate, needs principal sign-off)

If any primary series survives K1/K2 with positive pessimistic-bound net: run a
quote shadow on EC2 (compute quotes live, log would-be fills, **no orders**) for
≥5 sessions. Compare shadow net to backtest within ±30%. Divergence → back to E4
fill model. Pass → write micro-live proposal (size cap, kill switches) for
principal decision. Micro-live itself is NOT part of this experiment.

---

## Gates summary

| Gate | Test | Pass → | Fail → |
|---|---|---|---|
| G0 | fee model ratified vs real fills | E1–E2 decision-grade | results stay provisional |
| K1 (E2) | any cell edge > 0 after fees, n≥10k, t≥3 | E3–E5 | **kill sector** |
| K2 (E4) | pessimistic net > 0 somewhere | E6 | kill unless optimistic ≫ costs (then refine fill model once) |
| G3 (E7) | shadow within ±30% of backtest | micro-live proposal | iterate fill model once, else kill |

## Runs now vs needs deployment

| Piece | Data | Ready? |
|---|---|---|
| E0, E1, E2, E4, E5 | 16-day tape + L1 releases + settlements | **runnable today** |
| E3, E6 | H3 Coinbase capture (idle) + fresh Kalshi capture | needs H3 deploy + ~5 days accumulation |
| E4 refinement | crypto L2 | needs quota rotation decision |

## Stamped deviations & known pitfalls

1. Spread/depth medians currently from 07-07→09 window only; re-estimate on
   07-10→22 releases in E1 before use.
2. Queue simulation uses L1-qty proxy, not true L2 — pessimistic bound is the
   binding result until crypto L2 exists.
3. No retrospective Coinbase tick anchor — E2's to-settlement markout partially
   absorbs this; E3 fixes it prospectively.
4. 16–18 day history: all conclusions stamped "July 2026 window"; no regime claims.
5. `expt.duckdb` price-convention check is mandatory (E0) before any number is quoted.
6. **L1 tape is ~1Hz-conflated (T0 finding)** — all tape-based spread/lifetime/markout
   numbers are ≥1–2s resolution; sub-second claims require the crypto L2 delta capture.
7. ~~Maker-fee applicability unresolved~~ RESOLVED 2026-07-25: maker fee = 0 for
   all six series (official July PDF, see E0). Single-fee-case reporting from here on.

## Status log

- 2026-07-25: Parallel prod session (audit line) ran T0 census (14d, 37.8M trades)
  and E2 room map (79 cells, settlement-truth): early-window cells +0.46~+1.87¢
  net, final-2-min mid cells deep red (KXBTC15M mid −1.70¢) — "retail early,
  snipers late" confirmed in-grid. Discovery: **KXBTC15M full L2 already captured**
  (orderbooks_full Crypto/BTC, 07-12→23, ~8M events/day, recv_mono_ns, p50 gap
  2ms) — sub-second world visible for the main battlefield. Principal approved:
  E4 queue sim under the shadow-engine 10-point fill acceptance rules +
  known-answer test (graveyard cells must reproduce red), ms-level census first,
  8+4 TRAIN/VALIDATE split, H3 deploy + L2 quota +5 series today.
