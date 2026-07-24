# ALPHA-SPRINT-01 — Information-Edge Research Pivot

Date: 2026-07-24  
Mode: read-only research and shadow evaluation  
Financial authority: none  
Live orders: forbidden

## Decision

A01 spread capture is retained only as the shared PnL/execution acceptance
test. No further research time is allocated to proving that a displayed spread
exists. Work that remains useful to every strategy—exact fees, measured
latency, strict fills, complete exits, cash conservation and zero-opportunity
denominators—continues as shared infrastructure.

Primary research now targets information that can reach a fair-value estimate
before Kalshi's executable price fully adjusts:

1. real-time sports fair value;
2. event-to-market reaction delay;
3. crypto external-price anchoring;
4. order-flow continuation;
5. cross-market price deviation.

No missing sports state may be inferred from Kalshi price, order flow or market
labels. Missing external data blocks the relevant hypothesis; it is never
imputed.

## Shared strategy contract

Every admitted experiment must publish:

- a past-only signal with event time, source publication time, local receive
  wall time and local monotonic time where available;
- an exact mapping from source event/instrument/outcome to Kalshi ticker and
  YES/NO orientation;
- a deterministic action, venue, quantity, price cap, stop and exit;
- a no-trade baseline over the identical opportunity denominator;
- strict-fill entry and exit using effective-time executable depth;
- exact fees, slippage, latency and realized variable costs;
- pathwise cash PnL with zero-trigger, zero-fill and blocked rows retained.

The common estimand is:

`DeltaPnL = NetPnL_strategy - NetPnL_same_opportunities_no_trade`

where:

`NetPnL = cash_in - cash_out - exact_fees - variable_costs + terminal_value`

Markout, midpoint movement and model accuracy are diagnostics, not cash PnL.

## H1 — Sports real-time fair value

Registry lineage: `F13-EXTERNAL-SPORT-STATE`, `F12-EXTERNAL-ODDS-LEADLAG`,
and sport-specific children such as `C05-SOCCER-POISSON`.

Edge source:

An authoritative score, clock, possession/serve, lineup, injury or external
odds update changes the conditional payout probability before Kalshi's
executable quote incorporates it.

Required inputs:

- licensed point-in-time live sports state and/or timestamped sportsbook odds;
- provider event/publication timestamp plus local wall and monotonic receive
  timestamps;
- pre-event priors and sport-specific state transition/calibration data;
- versioned provider-event-to-Kalshi root/ticker/outcome mapping;
- synchronized Kalshi L1/L2/trades, exact fees and measured action latency.

Signal:

For each mapped outcome, calculate a calibrated probability interval
`[p_low, p_high]`. Buy YES only when `p_low - executable_yes_ask` exceeds
round-trip fees, impact, latency drift, model uncertainty and the frozen safety
buffer. Buy NO symmetrically from `1 - p_high`. Otherwise abstain.

Action:

One-contract marketable limit on Kalshi; cancel if the source is stale or the
mapping changes; exit at a frozen time horizon or fair-value reversal. Holding
to settlement is forbidden in the first validation revision.

Fastest validation:

Start with one simple mapped market class and an external consensus-odds fair
value before building a full sport-state model. Fit calibration on chronological
TRAIN only, seal it, and evaluate untouched later events with executable L2.

Failure:

The calibrated lower-bound edge is non-positive after costs, provider timing
cannot be proven, mapping coverage is too small, or the edge disappears on
untouched events.

## H2 — Event reaction delay

Registry lineage: `F12-EXTERNAL-ODDS-LEADLAG`,
`F13-EXTERNAL-SPORT-STATE`, `F14-PROVIDER-PRICE-LATENCY-TIER`.

Edge source:

A verified external state or odds shock reaches Kalshi with a measurable lag.
This is a latency event study, not a full fair-value model.

Required inputs:

- provider event time, publication time and local receive clocks;
- external update magnitude and direction;
- exact Kalshi receive-time L1/L2/trades;
- audited event/ticker/outcome mapping;
- measured decision-to-entry and decision-to-cancel latency.

Signal:

An external probability jump exceeds the frozen minimum while the mapped
Kalshi executable quote remains outside the post-update fair interval by more
than all costs at the earliest realistically actionable time.

Action:

Cancel a stale resting quote first. Directional entry is admitted only if the
mapping is exhaustive and the post-latency executable discrepancy remains
positive. Exit on Kalshi catch-up, source reversal or the frozen timeout.

Fastest validation:

Run an event study with no ML: align every verified external update to Kalshi,
plot response at 10ms/50ms/100ms/250ms/500ms/1s/5s, then apply actual latency
and L2. Use time reversal, unrelated-event and delayed-source placebos.

Failure:

The lead vanishes after publication/receive clocks, placebos match the effect,
or no discrepancy survives real action latency and costs.

## H3 — Crypto external-price anchor

Program boundary: new non-Sports child program. Existing
`F17-CRYPTO-SPOT-ANCHOR-OUT-OF-SCOPE` remains historically correct for the
Sports registry and is not silently reinterpreted.

Edge source:

The mapped settlement index and liquid external spot/order books move before a
Kalshi crypto threshold/range contract reprices, or the Kalshi implied digital
probability deviates from an index-, volatility- and time-consistent fair value.

Required inputs:

- exact Kalshi crypto rule, strike, close time and settlement-index identity;
- CF Benchmarks index values and the contract's trailing-average convention;
- public Coinbase and at least one independent exchange L2/trades;
- local wall/monotonic receive clocks and sequence/gap receipts;
- Kalshi executable L2, exact fees and measured latency;
- past-only realized-volatility and jump-risk estimates.

Signal:

Compute a conservative probability interval for the exact settlement event.
Trade only when the interval is wholly beyond the executable Kalshi price by
more than fees, impact, latency drift, basis risk and model uncertainty.

Action:

One-contract marketable limit in the underpriced Kalshi outcome; exit on
anchor convergence, model reversal or frozen timeout. The first revision does
not trade crypto spot or use leverage.

Fastest validation:

Capture BTC/ETH index, Coinbase/Kraken and Kalshi concurrently for 24–72 hours.
Begin with a transparent digital-probability baseline and compare it against
naive spot-distance and Kalshi-mid baselines before testing complex models.

Failure:

No lead remains after clock alignment, index/spot basis dominates the edge, or
fee-after executable PnL is non-positive.

## H4 — Order-flow continuation

Registry lineage: `B01-MOMENTUM-CONTINUATION`; model components may include
`A15-FAIR-MICROPRICE-PAST-FLOW`.

Edge source:

Signed aggressive trades, quote velocity, depth depletion and refill imbalance
contain short-lived information about the next executable price.

Required inputs:

- existing exact Kalshi trades, L1 and clean L2;
- taker-side orientation, exact exchange and local receive clocks;
- depth/depletion/refill state without gaps;
- exact fees and measured entry/exit latency.

Signal:

A past-only signed-flow score trained on simple, interpretable features predicts
a same-direction executable move larger than round-trip cost plus a frozen
safety buffer.

Action:

Buy YES for a positive score and BUY NO for a negative score using a
one-contract marketable limit. Exit at 100ms/1s/5s frozen horizons or on score
reversal, walking effective-time L2.

Fastest validation:

Use clean 2026-07-12 and 2026-07-15 as TRAIN and 2026-07-17 as this sprint's
chronologically held-out validation day. It is not represented as a globally
untouched confirmation set. Run a regularized logistic/linear baseline before
trees or sequence models. Retain all zero-trigger and no-depth opportunities.

Failure:

The validation sign or calibration is unstable, simple quote movement explains
the score, or executable fee-after PnL is non-positive.

## H5 — Cross-market price deviation

Registry lineage: `A10-CROSS-MARKET-QUOTE-SPILLOVER`,
`C04-SAME-EVENT-FAMILY-COHERENCE`, `C06-WITHIN-EVENT-LEADLAG`,
`C07-STALE-LEG`, with `C01/C02/C03` only where payout structure is proven.

Edge source:

Canonically linked contracts encode overlapping or exhaustive payouts but
update at different times or violate a statewise price constraint.

Required inputs:

- authoritative same-root membership, market rules and YES/NO orientation;
- exhaustive payout-state mapping for any multi-leg trade;
- synchronized effective-time L2 and quote-age clocks across every leg;
- exact multi-leg fees, serial-execution latency and orphan-unwind cost.

Signal:

Either a mapped leader moves while a follower remains stale, or a statewise
payout residual exceeds all-leg costs and worst-sequence orphan risk.

Action:

The first revision tests defensive cancel/reprice and publishes no directional
cash claim. A later revision may trade only the complete payout-verified vector,
never a convenient subset of legs.

Fastest validation:

First measure lead/lag on the already canonical same-event subset with
time-reversal and unrelated-root controls. In parallel, admit only families
whose payout matrix can be proven from rules; walk all legs under worst serial
ordering.

Failure:

Time reversal or unrelated roots match the effect, mapping is incomplete, or
fees/depth/orphan liquidation remove the residual.

## Execution order and time boxes

1. `H4 ORDER_FLOW`: existing data, first offline result.
2. `H5 CROSS_MARKET`: existing data on the verified mapping subset.
3. `H3 CRYPTO_ANCHOR`: start prospective capture immediately; first 24-hour
   diagnostic after one full day.
4. `H2 EVENT_LAG`: start when an external odds/state stream is available.
5. `H1 SPORTS_FAIR_VALUE`: begin with consensus odds, then add direct state
   models only after point-in-time feed validation.

Time-box rule: a hypothesis that cannot produce a signal-incidence,
executable-opportunity and falsification receipt by its declared deadline is
blocked and the research slot rotates. It is not kept alive by adding dates or
loosening thresholds.
