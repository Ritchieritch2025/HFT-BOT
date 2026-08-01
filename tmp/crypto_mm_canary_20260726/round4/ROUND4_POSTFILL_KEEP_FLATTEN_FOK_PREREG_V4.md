# ROUND4 Stage-2 public-proxy preregistration V4

Status: `PRE-DATA CONTRACT SEALED / DISCOVERY ONLY / NO CANDIDATE / NO LIVE`

This document freezes the historical Stage-2 data-generating mechanism for
`KXBTC15M-*`. It supersedes the V3 post-fill contract. V3 used
private/observed language for events that the available public raw history
cannot identify. V4 corrects the scientific claim; it does not authorize
extraction, fitting, candidate selection, shadow trading, or live trading.

The machine-readable authority is
`ROUND4_POSTFILL_KEEP_FLATTEN_FOK_PREREG_V4.json`.

## Sources and frozen inferences

- Kalshi [order directions](https://docs.kalshi.com/getting_started/order_direction)
  define the YES/NO bid/ask economics used by the canonical flatten mapping.
- Kalshi [Create Order V2](https://docs.kalshi.com/api-reference/orders/create-order-v2)
  documents `fill_or_kill`, `reduce_only`, and self-trade-prevention fields.
- The official [Kalshi Fee Schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf),
  effective July 7, 2026 and checked July 26, 2026, assigns the event-contract
  maker formula a default multiplier of zero unless a series is listed with a
  nonzero multiplier. `KXBTC15M` is not in its non-standard list. V4 therefore
  freezes first-leg and complement maker fees at zero for this series. The
  schedule also states that settlement has no fee.
- Kalshi's [market-outcome explanation](https://help.kalshi.com/en/articles/13823826-market-outcomes)
  says a market is determined from its contract rules and named source.
  Kalshi's [prediction-market explanation](https://help.kalshi.com/en/articles/13823766-what-are-prediction-markets)
  states that a correct standard binary contract is worth $1.

The zero-maker conclusion is a schedule-based inference, not a private fee
receipt. The schedule must be checked again before using this contract on
future dates or in deployment.

## Data and provenance boundary

Only public raw discovery rows from UTC dates `2026-07-20`, `2026-07-21`, and
`2026-07-22` are allowed. Dates `2026-07-23` and `2026-07-26` are forbidden.
Every logical path is checked before opening; one invalid row rolls back the
complete market-day.

Every V4 row serializes its origin and applicable provenance:

| Mechanism | Frozen value |
|---|---|
| Data origin | `PUBLIC_RAW` |
| Historical first/complement fill | `PUBLIC_STRICT_TRADE_THROUGH_FULL_PROXY` |
| Historical maker execution nature | `SYNTHETIC` |
| Historical maker fee | `SCHEDULE_ZERO_MAKER` |
| FOK execution | `PUBLIC_BOOK_COUNTERFACTUAL` |
| Cancel timing | `SYNTHETIC_60MS_SCENARIO` |
| Self-cross scope | `SIMULATED_STRATEGY_ORDER_REGISTRY` |
| Market-close reward | `OFFICIAL_RESULT_SETTLEMENT_SIMULATION` |
| Settlement fee | `SETTLEMENT_ZERO_FEE` |

`SIMULATION_EXACT` means exact arithmetic conditional on one of these
serialized mechanisms. It never means that a private execution, private fee,
or historical cancel acknowledgement was observed.

The public strict-trade-through rule recognizes only full proxy fills.
Historical partial maker fills are not identified. Consequently, every state
that remains at risk must satisfy:

```text
remaining_inventory_fp = requested_qty_fp = first_fill_qty_fp
```

## Decision state

The sealed grid is:

```text
0, 250, 500, 1000, 2000, 5000, 10000, 30000, 60000 ms
```

A terminal at the same timestamp wins over the grid. A same-envelope pair at
elapsed zero is serialized only as `postfill_zero_time_atom`.

`round4_postfill_state_contract.sql` is the normative V4 schema. V4 reuses
only the `postfill_zero_time_atom` table and its public-provenance extension
from `round4_two_stage_tables.sql`. Other tables in that shared historical V1
file—including any old admin or acknowledgement vocabulary—are nonnormative
and forbidden as V4 training input.

Each at-risk point has one state fingerprint, two actions, one KEEP
transition, and one FOK outcome:

```text
KEEP
FLATTEN_FOK
```

Touch-related missing fields use explicit indicators and an all-present or
all-NULL bundle. Stale carry and extractor imputation are forbidden.

Historical primary kernel fair is always unavailable. `kernel_fair_available`
must be false and all seven kernel fields must be `NULL`, because the external
five-second source-time value has no raw receive timestamp that makes it
causal at the decision receipt. It may be studied separately only as
`NON_CAUSAL_SENSITIVITY`; it cannot enter a candidate or deployment gate.

## KEEP

KEEP leaves the original complement maker order unchanged:

```text
same complement_order_id
same complement_price_e4
no replace
no repricing
```

Public queue position and flow may evolve causally, but the order's identity
and price do not reset. KEEP has only two terminals:

1. `COMPLEMENT_FILL`: the public strict-trade-through full-fill proxy reaches
   the original order at its original price.
2. `HARD_FALLBACK`: only at market close, using the official YES/NO result,
   binary payout arithmetic, and zero settlement fee.

`CURRENT_DIST2_TTL60` is an independent comparator, not a KEEP terminal.
`ADMIN_CENSOR` is forbidden in training.

## FLATTEN_FOK

The canonical V2 mapping is:

```text
held YES -> ASK -> sell YES -> walk YES bids from best to worse
held NO  -> BID -> buy YES  -> walk YES asks from best to worse
```

The action covers all remaining inventory, is `reduce_only`, uses
`fill_or_kill`, and uses `taker_at_cross` as a protocol backstop. A full
decision-time public-book walk freezes the worst full-clip price plus one
adverse legal tick. Incomplete or empty visible depth freezes the adverse
legal boundary and sets `flatten_limit_fallback=true`.

The 60ms timing input comes from:

```text
/home/ubuntu/hft-bot/work/latency/receipts/concurrent_cancel_1784993947.json
size:   217 bytes
sha256: 8d460e6b4e8dbfa87ef373b21e34bb41e76b0bcf3f881c46f817a04c51f31e08
sample: 10
```

It is a conservative synthetic scenario, not an observed acknowledgement, a
measured p99, or a production SLA. At the scenario's effective timestamp:

```text
sequence 0: synthetic cancel applied
sequence 1: public-book FOK counterfactual processed
then:       same-time public crossing event is not applied
```

The no-self-cross check covers only orders in the simulated strategy registry.
It makes no claim about every order in a real account or subaccount.

FOK outcomes are:

- `CANCEL_RACE_PAIR`: the public complement full-fill proxy occurs strictly
  before synthetic cancel application; no FOK is sent.
- `FOK_FULL`: the public-book counterfactual fills the complete clip.
- `FOK_ZERO`: it fills zero, retains all inventory, and keeps capital at risk
  through market close.

Partial FOK is forbidden.

## Economics

Let `q` be quantity; `p0` the first-fill dollar price; `pc` the unchanged
complement dollar price; `(pi, qi)` FOK slices; `S` the held side; and `R` the
official result.

```text
first cost                       = q * p0
KEEP/race pair gross             = q * (1 - p0 - pc)
FOK gross, held YES              = sum_i qi * (pi - p0)
FOK gross, held NO               = sum_i qi * (1 - pi - p0)
settlement gross                 = q*(1-p0) if R=S else -q*p0
first/complement maker fee       = 0
settlement fee                   = 0
raw FOK taker fee per slice      = 0.07 * qi * pi * (1-pi)
```

The FOK taker-fee precision band is:

```text
low  = ceil(sum(raw slice fees), $0.0001)
base = sum(ceil(each raw slice fee, $0.0001))
high = sum(ceil(each raw slice fee, $0.01))
```

Thus:

```text
FOK_FULL net band         = gross - taker-fee band
FOK_ZERO lower bound      = -first cost
race/KEEP complement net  = pair gross
```

Capital accounting is:

```text
race/FULL dollar-seconds = first_cost * terminal_ms / 1000
ZERO dollar-seconds      = first_cost * market_close_ms / 1000
KEEP interval increment  = first_cost * (stop_ms - start_ms) / 1000
```

## Fail-closed seal

Python and DuckDB independently recompute inventory, fee, PnL, provenance, and
capital identities. Tests must accept FULL, ZERO, RACE, market-close HARD, and
zero-time atom rows, while rejecting at least:

- private fill provenance;
- observed-cancel provenance;
- historical kernel availability;
- nonzero maker or settlement fees;
- a real-account-wide self-cross claim;
- `CURRENT_DIST2_TTL60` as a KEEP terminal;
- HARD fallback before market close;
- partial inventory or partial FOK;
- a non-`KXBTC15M-*` ticker under this fee assumption.

Passing this seal authorizes no candidate and no live strategy. The V4 learner
must later pin the new contract and pass its own tests before fitting can even
be considered.
