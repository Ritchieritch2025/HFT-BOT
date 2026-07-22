# C1 Real-Fill Kill Test — Frozen Specification

**Experiment ID:** `C1-REAL-FILL-2026-07-22.01`  
**Frozen:** 2026-07-22  
**Audit repair:** repair-01 on 2026-07-22, before the full run; post-fill exit
depth and partial-identification bounds were added after an adversarial sample
found that the point-estimate wording could overstate executable quantity.
**Phase advanced:** Phase 1.5, candidate falsification only  
**Execution:** W09 read-only research; no exchange writes, no live orders  
**Claim ceiling:** `EXPLORATORY_NON_GATE` until every fee, latency, and terminal
fact is exact.

## 1. Decision this experiment is allowed to make

Deepresearch V3 found that, on the three clean L2 dates, 50.5%–57.2% of
displayed depletion episodes showed an 80% refill within one second. That is
not a fill probability. This experiment asks the narrower question:

> If one passive one-contract order had been activated at the depleted side's
> original touch, how often does the public trade tape prove it would have
> filled, and what executable gross markout followed?

The experiment may:

- kill C1 when even the binding strict-through gross evidence is economically
  adverse or too sparse;
- retain C1 as a candidate for more data when the result is promising but a
  fee/latency/terminal dependency remains;
- report queue width and adverse-selection diagnostics.

It may **not** claim net PnL, daily profit, capacity, G3 promotion, or readiness
for live trading. Three dates are a pilot, not the required 20-day validation.

## 2. Exact immutable inputs

The consumer must validate the following externally frozen hashes before
opening any Parquet input. It must never discover a hash on W09 and then trust
the discovered value.

| Input | Exact binding |
|---|---|
| Successful runtime | `5d5d27836891ae22cebecae85f90f2674e579909` |
| Source binding | `c08083fe0f6a05cb60f95f1eee70d61a8075792e83c8f72c62927254c36b1979` |
| Upstream `INPUT_MANIFEST.json` | `e51fac1d9ffd27c4a45bc8b685191e058b4138f470dd83a1719e5e13b729a927` |
| Upstream L2 execution receipt | `61992c459883291011f53b538e1fdf3f1015a4f82cc367880db405b878fc9f51` |
| `l2_availability/MANIFEST.json` | `bb9c56fddaa86372902fc4d10317e49b4885cf814fef7647e40f4bc8266ed28b` |
| `l2_replay/MANIFEST.json` | `3cf24b5a28d27badd2fcd1638ac14e2fe27f2558a9a90e377b7eedf6a84a48f2` |
| `l2_episodes/MANIFEST.json` | `ecdc27a2cd64ba456683d4db12c0128fbfb0f4d60b58a7960f7f327b4163d89a` |
| `trades_market/MANIFEST.json` | `5d963ea39487c0991f3f759fa2d6872e0d50deadd5fbacc41e73e8a2a62f01ee` |
| `dim_market_date/MANIFEST.json` | `7af45436bf2bc41b6f94ccc6c11f44f70ce5d4f18fe62fb6a7666010f7def6dc` |

Only `2026-07-12`, `2026-07-15`, and `2026-07-17` are estimand dates. Every
other date must appear in the exclusion waterfall. The universe is the
captured targeted watchlist, not the whole exchange.

The checkpoint namespace is opened by a new read-only validator. The existing
`BoundedCheckpointStore` is forbidden because entering it takes a writer lock.
Every manifest, partition receipt, payload size, payload SHA, schema, partition
set, and row count is verified before use.

## 3. Trigger and campaign

The trigger is inherited byte-for-byte in meaning from Deepresearch V3:

- a sequence-valid `DELTA_APPLIED` row;
- a negative delta at the pre-update same-side top price;
- pre-touch quantity at least one contract (`10_000` CountE4);
- removed quantity at least one contract and at least 50% of pre-touch;
- snapshot rows never trigger and never prove refill.

`t0` is the trigger's receive-wall clock. The quote is one contract
(`10_000` CountE4), on the depleted `yes` or `no` side, at
`original_touch_price_e4`. Results never use a refill that occurs after `t0`
to decide whether the quote was placed.

Only one campaign can exist per market in `[t0, t0 + 1 second]`. Later triggers
inside that interval are `OVERLAP_SUPPRESSED`. This prevents one public trade
from filling several counterfactual orders.

## 4. Frozen policy variants

No value may be selected after observing results.

- Placement/cancel latency scenarios: `FAST=5/5ms`, `PRIMARY=50/50ms`,
  `STRESS=500/500ms`. They are placeholders, so every economic result is
  `NON_GATE`.
- No-refill cancel timers: `200ms` and `300ms` from `t0`.
- If 80% displayed refill is observed by the timer, cancel intent moves to
  `t0 + 1s`; otherwise it fires at the timer.
- Effective live interval is `(t_active, t_cancel_effective]`. A trade stamped
  in the activation microsecond is rejected; a trade stamped at the
  cancel-effective microsecond remains fillable.
- Activation requires the same valid snapshot epoch, a two-sided uncrossed
  book, a past-only state no older than 250ms, and a post-only quote.

Because the trade checkpoint retains only microsecond receive time, ambiguous
same-microsecond order is always resolved against the strategy.

## 5. Three fill tracks

All prices and quantities remain fixed-point integers. Only globally deduped
public trades can create a fill. L2 negative deltas never create a fill.

For a public trade, convert it to the resting side it consumed:

```text
taker_side=no  -> resting_side=yes, resting_price=yes_price_e4
taker_side=yes -> resting_side=no,  resting_price=10000-yes_price_e4
```

Unknown side, price-complement failure, non-positive size, conflicting trade
identity, or unsorted input fails closed.

### Binding strict-through

This is the headline fill lower bound and the only track eligible to kill or
retain the candidate.

- `resting_price == quote_price`: no fill.
- `resting_price < quote_price`: fill at most
  `min(order_remaining, public_trade_size)`.
- A print never fills more quantity than it actually reports.

### Queue-pessimistic diagnostic

At activation, queue ahead is the exact displayed quantity at the quote price.
If that exact quantity cannot be reconstructed from the activation touch, the
episode is `QUEUE_AHEAD_UNKNOWN` for this track.

- same-price trades consume queue ahead before the virtual order;
- negative deltas do not reduce queue ahead;
- new same-price positive deltas after activation are behind the order;
- strict-through trades fill as above and clear remaining queue uncertainty.

### Optimistic-at-touch diagnostic

The order is assumed at the front of its price level. Same-price and
strict-through public trades may fill it. This is an upper bound and is never
used in the headline, gate, or recommendation by itself.

## 6. Outcomes

For every fill slice, use same-epoch, sequence-valid, past-only L2 state to
measure 100ms, 200ms, 500ms, and 1000ms markouts. The executable gross
liquidation value is the then-current best bid for the held outcome:

```text
long YES: gross_e4 = future_yes_bid_e4 - entry_yes_price_e4
long NO:  gross_e4 = future_no_bid_e4  - entry_no_price_e4
```

Midpoint markout is published as a diagnostic only. The future state must be
strictly later than the public fill print; a state in the fill microsecond or
earlier is censored against the strategy. Missing, stale, invalid,
cross-epoch, or crossed future state is censored, never forward-filled.

The executable component is capped by displayed same-outcome exit depth at the
future touch. For a fill slice of quantity `F` and future exit depth `Q`:

```text
observed_quantity = min(F, Q)
censored_quantity = F - observed_quantity
```

Only `observed_quantity` receives the future executable-bid markout. This is a
per-slice liquidity diagnostic, not a simultaneous portfolio-capacity claim.
The censored quantity remains in the ledger and can never silently inherit the
observed price.

Displayed refill/no-refill is an ex-post attribution column only. The report
must separate:

- strict fills in refill episodes;
- strict fills in non-refill/censored episodes;
- executable gross markout for each group;
- fill-track width (`optimistic - queue - strict`);
- exclusions and unresolved fee coverage.

Fee-after net PnL is forced to `NOT_ESTIMABLE`. The exact releases do not bind
fill-time historical fee changes, all event overrides, account precision, or
the per-order rounding accumulator. Gross results must not be relabeled net.

## 7. Statistical and decision rules

The unit count is not treated as independent evidence. Publish date, event,
market, maximum-market share, and HHI. With three dates, report each date and
direction consistency; no t-test, p-value, or significance language is
allowed.

Pilot verdict precedence (audit repair-01, fixed before the full run):

1. `METHODOLOGY_INVALID` for conservation, dedupe, clock, or hash failure.
2. `KILL_C1_ENTRY` if strict fills are zero. Otherwise, each required
   `PRIMARY x date x cancel-timer x {200ms,1000ms}` cell receives a
   partial-identification interval. Observed quantity contributes its exact
   executable gross markout; each censored unit contributes the mechanically
   valid interval `[-entry_price, 10000-entry_price]`. C1 is killed only when
   the *upper* gross bound is non-positive in every required cell.
3. `RETAIN_FOR_20_DAY_VALIDATION` only when the *lower* gross bound is strictly
   positive in every required cell and strict fill support is nonzero on every
   date.
4. Otherwise `INDETERMINATE_MORE_CLEAN_DAYS`.

No coverage percentage is selected after observing the pilot. Every cell must
publish total, observed, and censored quantity plus its integer weighted gross
lower/point/upper sums and the exact weighted entry-price sum for censored
quantity. The publisher must mechanically recompute `lower = point -
censored_entry_sum` and `upper = point + 10000*censored_quantity -
censored_entry_sum`; zero observed quantity forces zero point and midpoint
sums. This repair replaces the earlier point-estimate-only wording because an
independent pre-run audit proved that sparse observed depth could otherwise
force either a false retain or a false kill.

No positive pilot verdict is promotion. The G3 fee-after threshold remains
blocked until exact fees, measured latency, a complete exit/terminal ledger,
and at least 20 clean dates exist.

## 8. Required artifacts and acceptance

The run must produce input/spec/clock/queue/fee/exclusion/resource/
reproduction receipts, all 48 partition receipts, fill slices, aggregate
tables, chart source CSVs, `C1_RUN_COMPLETE.json`, an artifact SHA ledger, an
HTML report, and a rendered/visually inspected PDF.

Acceptance requires:

- every frozen hash and row conservation check passes;
- no public trade ID is allocated more than once per fill track/variant;
- filled quantity never exceeds eligible public trade quantity;
- every executable markout uses a strictly post-fill state and never exceeds
  displayed exit depth; observed plus censored quantity equals filled quantity;
- the 48-partition merge independently recomputes global trade-allocation and
  public-volume conservation rather than trusting partition receipts;
- the pilot verdict is recomputed from published integer lower/upper bounds;
- the publisher verifies the analysis artifact ledger byte-for-byte, including
  all 48 partition receipts and their campaign/fill/markout/exclusion files,
  before generating any chart or report completion receipt;
- two identical replays produce byte-identical canonical tables and verdict;
- tests cover side complement, at-price strict rejection, partial fills,
  exact queue exhaustion, same-time boundaries, duplicate/conflicting IDs,
  overlap suppression, snapshot/epoch invalidation, and post-only rejection;
- live-order write count is exactly zero.
