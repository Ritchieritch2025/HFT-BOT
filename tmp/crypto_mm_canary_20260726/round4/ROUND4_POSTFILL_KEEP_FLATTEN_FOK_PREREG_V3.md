# ROUND4 post-fill KEEP / FLATTEN_FOK preregistration V3

Status: `PRE-DATA CONTRACT SEALED / DISCOVERY ONLY / NO LIVE`

This document is self-contained. It freezes the Stage-2 state, action,
transition, outcome, clock, fee, and rollback semantics before any V3
extractor may open real ROUND4 rows. It does not authorize extraction, model
fitting, candidate selection, shadow trading, or live trading.

## Scope and source boundary

Only UTC discovery dates `2026-07-20`, `2026-07-21`, and `2026-07-22` are
allowed. Dates `2026-07-23` and `2026-07-26` are forbidden. Every logical
source path is preflighted before it is opened. One invalid row rolls back the
complete market-day; no partial batch is returned.

The authority clock is strict local receive wall/monotonic time. A terminal at
the same timestamp as a grid point wins, so it produces no state at that grid.
The at-risk grid is:

```text
0, 250, 500, 1000, 2000, 5000, 10000, 30000, 60000 ms
```

A zero-time paired fill is only a `postfill_zero_time_atom`. It has no state,
action, transition, or artificial epsilon-duration interval.

The zero-atom table is reused only from `round4_two_stage_tables.sql`, SHA-256
`ede23569a00e9948cd3772cc2213a07f8e5c3b9849b9dd0f46e8e6adff3eaed3`.
No other pre-V3 action table in that shared file is normative for V3.

## One state, two actions

Each at-risk grid has one causal state fingerprint and exactly two actions:

```text
KEEP
FLATTEN_FOK
```

`KEEP` leaves the resting complement order post-only. `FLATTEN_FOK` cancels
that order, waits for cancel ACK, proves that no other own order can cross in
the same market/subaccount, then sends one reduce-only `fill_or_kill` order for
all remaining inventory. Risk exit bypasses the pair-profit gate.

The official direction mapping is:

```text
held YES -> V2 ASK -> sell YES -> walk YES bids descending
held NO  -> V2 BID -> buy YES  -> walk YES asks ascending
```

This mapping follows Kalshi's
[order-direction documentation](https://docs.kalshi.com/getting_started/order_direction).
The V2 order fields follow the official
[Create Order V2 documentation](https://docs.kalshi.com/api-reference/orders/create-order-v2).

The self-trade-prevention setting is frozen to `taker_at_cross`, but that is a
backstop. It never substitutes for the no-self-cross proof.

## Missing-book and delayed-kernel state

Queue, same-price-ahead, better-depth, and 1s/5s/10s/60s flow fields remain
required even when touch is absent.

Three explicit missing indicators govern every nullable model field:

| Indicator | Fields that must all be present or missing |
|---|---|
| `two_sided_touch_available` | `touch_imbalance`, `spread_e4`, `mid_move_1s_e4`, `mid_move_10s_e4`, `mid_move_since_entry_e4`, `mid_move_since_fill_e4` |
| `complement_touch_available` | `complement_touch_distance_e4` |
| `kernel_fair_available` | `kernel_fair_e4`, `first_leg_fair_edge_e4`, `complement_fair_edge_e4`, `kernel_fair_move_since_entry_e4`, `kernel_fair_move_since_fill_e4`, `kernel_source_time_ms`, `kernel_causality_kind` |

Two-sided touch implies complement-side touch. When an indicator is false, its
entire bundle is `NULL`; stale carry is invalid. The extractor never imputes.
The learner may apply train-fold-only median imputation plus the matching
indicator to the twelve sealed nullable numeric model fields. Kernel source
time and causality kind are audit-only.

Historical and future-live kernel state uses exactly
`SOURCE_TIME_MINUS_5000MS`:

```text
kernel_source_time_ms <= decision_wall_ms - 5000
```

Missing kernel history is legal and yields the all-`NULL` bundle. When present,
the contract recomputes the leg edges. For example, fair `63.49c` and a first
YES bought at `70c` gives `-6.51c`, not a positive edge.

## Canonical limit and empty books

KXBTC15M uses the tapered legal grid:

```text
10..9990 e4
below 1000 or above 9000: step 10
1000 through 9000: step 100
```

If decision-time canonical depth fills the whole clip, the limit is the worst
filled level plus one adverse legal tick, capped at the legal boundary.
Boundary tests are `ASK 1000 -> 990` and `BID 9000 -> 9010`.

If depth is incomplete—including a one-sided or empty book—the limit is the
most adverse legal boundary and `flatten_limit_fallback=true`. Executable
quantity plus residual inventory must equal remaining inventory.

## Cancel / FOK receive-time protocol

Total decision-to-effective latency is frozen at `60ms`. Its engineering
receipt is:

```text
/home/ubuntu/hft-bot/work/latency/receipts/concurrent_cancel_1784993947.json
size:   217 bytes
sha256: 8d460e6b4e8dbfa87ef373b21e34bb41e76b0bcf3f881c46f817a04c51f31e08
```

That receipt contains only ten observations. The `60ms` value is a
conservative research scenario, not a measured p99 and not a production SLA.

At the exact effective timestamp, ordering is:

```text
cancel ACK (sequence 0)
FOK processed (sequence 1)
public crossing event (not applied to the ACK book)
```

After cancel ACK and verified no-self-cross, the FOK is atomic:
`FOK_FULL` or `FOK_ZERO`. A missing no-self-cross proof at send time produces
`SELF_CROSS_UNIDENTIFIED`, rolls back the market-day, and emits no normal
outcome.

## Outcomes and inventory conservation

`CANCEL_RACE_PAIR` exists only when the exact private resting-complement fill
occurs strictly before cancel ACK. It must fill all remaining inventory,
leave zero residual, and carry an exact private price, quantity, stable receipt
id, and maker-fee receipt. No FOK is sent.

An identified partial private maker fill is unsupported `DATA_INVALID`; it
cannot be mislabeled as a pair. Historical strict trade-through may identify
only full fills, so every V3 report must disclose that historical private
partial fills were not identified. A future private-feed test must validate
this path separately.

Because those partial private fills are not identified, an episode that remains
at risk cannot quietly shrink across grids:

```text
remaining_inventory_fp = requested_qty_fp = first_fill_qty_fp
```

Any smaller or decreasing at-risk inventory is unsupported `DATA_INVALID`.

`FOK_FULL` fills the entire requested clip and leaves zero residual.
Private fill partition is not known from public L2 levels, so its reward is a
fee band and is never labeled exact.

`FOK_ZERO` fills zero and retains the complete residual inventory:

```text
conservative_reward
  = -(first_leg_cost_basis_usd + first_fill_fee_usd)
```

Capital remains held to market close, `reward_value_kind=LOWER_BOUND`,
`reward_exact=false`, and `conservative_fit_usable=true`. A selected out-of-
sample policy containing any `FOK_ZERO` is `NO_CANDIDATE`. Zero outcomes may
not be dropped or converted into partial executions.

## KEEP transitions

Every state also has exactly one KEEP transition. Before the final boundary:

```text
next grid occurs first -> NEXT_STATE, next_decision_id set, immediate reward 0
observed terminal occurs first -> KEEP_TO_OBSERVED_TERMINAL
```

If inventory remains at risk at `60000ms`, KEEP continues only from that final
state to the frozen old-policy observed terminal or hard-fallback receipt.
There is no fabricated next grid. The exact terminal reward and interval
capital are booked once.

`CURRENT_DIST2_TTL60` must replay the exact historical policy receipt
separately; it is not a grid-snap approximation.

## Economics and first-fee conservation

For FOK slices `(p_i, q_i)`:

```text
held YES gross = sum q_i * (p_i - first_yes_price) / 10000
held NO  gross = sum q_i * (10000 - p_i - first_no_price) / 10000
pair gross     = q * (10000 - first_price - complement_price) / 10000
raw taker fee  = 0.07 * q * p * (1 - p)
```

Fee sensitivity is:

```text
low  = ceil(sum(raw level fees), $0.0001)
base = sum(ceil(raw level fee, $0.0001))
high = sum(ceil(raw level fee, $0.01))
net  = gross - first-fill fee - taker-fee band
```

The first-fill fee is counted exactly once:

- `FOK_FULL`: first-fill fee in `maker_fee_usd`; exit fee in the taker band.
- `FOK_ZERO`: first-fill fee in `maker_fee_usd` and in the conservative
  cost-basis lower bound, not twice in either reward.
- `CANCEL_RACE_PAIR` and paired KEEP terminal: first-fill fee plus exact
  complement maker fee.
- Admin/hard-fallback KEEP receipt: reported first-fill fee must equal the
  episode receipt, and total fee must equal first-fill plus exit fee.

All low/base/high net values reconcile separately and remain monotone.

The serialized state, FOK outcome, and KEEP transition each carry their own
authoritative first-fill price, quantity, fee, and cost basis. KEEP additionally
carries `exit_fee_usd`. This lets the supplemental DDL reject forged rows
without relying on Python or a cross-table join:

```text
cost_basis = first_price * first_qty / 10000
FOK_FULL/ZERO maker_fee = first_fill_fee
CANCEL_RACE maker_fee = first_fill_fee + race_complement_fee
FOK_ZERO conservative_reward = -(cost_basis + first_fill_fee)
KEEP terminal maker_fee = first_fill_fee + exit_fee
KEEP net = gross - maker_fee
```

## Seal gates

The complete batch must pass roster, grid, paired-state, clock, inventory,
first-fee, fee-band, touch/kernel missingness, no-self-cross, transition, and
one-outcome-per-action checks. No V3 real row has been opened and no remote V3
run has started.

Local verification at this revision:

```text
python3 -m pytest tests/test_round4_postfill_state_contract.py -q
30 passed
```
