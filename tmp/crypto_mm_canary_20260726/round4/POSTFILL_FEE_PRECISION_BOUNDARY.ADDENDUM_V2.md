# Post-fill fee precision boundary — superseding addendum v2

Status: **DISCOVERY ONLY / NO CANDIDATE / NOT DEPLOYABLE / NOT LIVE AUTHORIZED**

Classification:
`OPTIMISTIC_L2_AGGREGATE_MIN_FEE_MAX_PNL_SENSITIVITY`.

This addendum corrects the precision claim in the draft v1 fee sidecar. It
does not change any episode, action, selected policy, exit timestamp, capital
time, maker result, IOC book timestamp, or L2 book walk.

## What is established

The sealed replay used a generic once-per-order whole-cent fee:

```text
raw_order_fee = sum_j 0.07 * quantity_j * price_j * (1 - price_j)
old_fee       = ceil_to_$0.01(raw_order_fee)
```

This is directly established by lines 139–148 of
`z3_price_allocation_train.py`, SHA-256
`81c35b2982db9d892fb2c97d55f572681d5a60631b6367023e9330cdfc0feb5b`.

The deterministic sidecar calculation is:

```text
l2_aggregate_fee = ceil_to_$0.0001(raw_order_fee)
l2_aggregate_pnl = old_pnl + old_fee - l2_aggregate_fee
```

It is an optimistic minimum-fee / maximum-PnL point sensitivity over the
frozen L2 price-level slices. It is exactly reproducible as that sensitivity,
but it is not an exact current-account realized fee.

## Official current mechanics and the identification boundary

Kalshi's current
[Fee Rounding documentation](https://docs.kalshi.com/getting_started/fee_rounding)
(accessed 2026-07-26; fetched Markdown SHA-256
`c9b8c7efd50df6512a4528e5a86044ad40699e17ababc7f9c42497c722829796`)
specifies:

1. Each true fill's quadratic trade fee is rounded up to `$0.0001`.
2. `balance_change = signed revenue - trade fee` is floored toward negative
   infinity to the account's target precision; a direct member's target is
   `$0.0001`.
3. The difference is a rounding fee.
4. A per-order accumulator spans all fills and may return whole-cent rebates;
   net fee remains non-negative.

The frozen trajectory contains L2 aggregate quantity at each price level. It
does not contain the private same-price counterparty fill partition or its
per-order rounding-accumulator path. Therefore these artifacts do not identify
an exact current-account point fee or point PnL. Private `fee_cost` fill
receipts, `order_id` grouping, and balance/position fee identities would be
required for that claim.

## Partition-independent proof

Let `r_i` be the unrounded fee of each unobserved true fill and let
`A = ceil_$0.0001(sum_i r_i)` be the L2 aggregate point fee.

```text
sum_i ceil_$0.0001(r_i) >= ceil_$0.0001(sum_i r_i) = A
```

After all order-level rebates, the remaining rounding residue is non-negative.
Thus the actual direct net fee is at least `A`. If
`O = ceil_$0.01(sum_i r_i)` is the sealed old fee, then:

```text
PnL_true - PnL_old = O - fee_true <= O - A <= $0.0099 = 0.99c per IOC
```

This upper bound does not require the missing fill partition. Maker-pair rows
have no IOC fee and remain unchanged.

Using the exact sealed row means and the frozen IOC counts (`N=1202`), every
executable fixed policy remains negative even at this maximum possible
improvement:

| Policy | Old exact EV c/first fill | IOC rows | Strict EV upper c/first fill |
| --- | ---: | ---: | ---: |
| IOC_60MS | -1.626134 | 1143 | -0.684728 |
| WAIT_0P25 | -1.548799 | 1029 | -0.701286 |
| WAIT_0P5 | -1.467260 | 953 | -0.682344 |
| WAIT_1 | -1.452657 | 867 | -0.738572 |
| WAIT_2 | -1.284293 | 719 | -0.692105 |
| WAIT_5 | -1.318082 | 580 | -0.840378 |
| WAIT_10 | -1.422202 | 458 | -1.044981 |
| WAIT_30 | -1.653744 | 289 | -1.415715 |
| WAIT_60 | -1.651447 | 229 | -1.462836 |
| CURRENT_DIST2_TTL60 | -1.064875 | 574 | -0.592113 |
| CLAIRVOYANT_BEST_FIXED | 0.604478 | 284 | 0.838388 |

`CLAIRVOYANT_BEST_FIXED` remains timing headroom only. It is not an executable
policy, candidate, validation result, or deployment authorization.

## Superseded draft-v1 wording

The following descriptions are withdrawn wherever they refer to hypothetical
L2 book walks:

- `authenticated direct-account centicent fee`
- `exact current-account fee repricing`
- `exact direct-centicent fees`
- `direct realized fee` or equivalent

They are replaced by:
`optimistic L2-aggregate minimum-fee / maximum-PnL sensitivity`.

Only a private-fill-and-balance reconciliation may be labeled exact current
account. The strict `0.99c/IOC` improvement ceiling above remains valid.

## Frozen source receipts

- Original report:
  `ff5c990cbb05e3695604c9c61612d7e8e92aeced3d4b3e12b3417e8220265cc8`
- Original episodes:
  `b4440b5822416954134f656dfe2aaf61fa3e8be0cfcdd295e62fc19f6bf3201f`
- Sealed replay:
  `b667c811707b7203bc532b5d51accc1ff2e082d2fff284e76b7377e8945d1b09`
- Preregistration:
  `27549b7278eaa414bc2d4c06464ae881846d36ff8f281dd3f56678a9e098a0b7`
- `date_2026_07_23_read = false`

Decision locks: `candidate=false`, `deployable=false`,
`live_authorized=false`.
