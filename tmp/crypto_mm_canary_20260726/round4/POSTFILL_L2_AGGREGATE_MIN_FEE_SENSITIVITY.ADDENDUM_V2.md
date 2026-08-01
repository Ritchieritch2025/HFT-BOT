# Post-fill discovery: fee-precision boundary addendum

Status: **POST_HOC L2-AGGREGATE FEE SENSITIVITY / NO CANDIDATE / NOT LIVE AUTHORIZED**

This addendum does not rerun policy selection. It holds every episode, policy, exit timestamp, maker result, capital time, and IOC book walk fixed. Its point estimate replaces only the generic whole-cent IOC fee with a once-per-order centicent ceiling over the frozen L2 book-walk slices.

**Precision boundary:** that point estimate is not a private-fill-exact current-account fee. Current Kalshi documentation ceilings trade fee per true fill, then applies direct-member $0.0001 balance rounding plus an order accumulator/rebate. The L2 trajectory does not reveal how one price-level quantity partitions across resting counterparty orders. Exact current-account point PnL is therefore not identified by these artifacts.

## Integrity result

- Original report SHA-256: `ff5c990cbb05e3695604c9c61612d7e8e92aeced3d4b3e12b3417e8220265cc8`
- Original episodes SHA-256: `b4440b5822416954134f656dfe2aaf61fa3e8be0cfcdd295e62fc19f6bf3201f`
- Old IOC PnL reproduced: `7125/7125`
- Markout old PnL reproduced: `10818/10818`
- Maker rows byte-identical: `6097/6097`
- Action projection identity: `True`
- 2026-07-23 read: `false`

## Frozen L2-aggregate fee sensitivity

| Policy | IOC exits | Old EV c/first fill | L2 aggregate EV | 95% market-cluster CI | Partition-independent strict upper bound |
| --- | ---: | ---: | ---: | ---: | ---: |
| IOC_60MS | 1143 | -1.6261 | -1.1869 | [-1.2957, -1.0732] | -0.684728 |
| WAIT_0P25 | 1029 | -1.5488 | -1.1517 | [-1.2970, -0.9975] | -0.701286 |
| WAIT_0P5 | 953 | -1.4673 | -1.1044 | [-1.2726, -0.9487] | -0.682344 |
| WAIT_1 | 867 | -1.4527 | -1.1135 | [-1.3111, -0.9348] | -0.738572 |
| WAIT_2 | 719 | -1.2843 | -1.0036 | [-1.2067, -0.8155] | -0.692105 |
| WAIT_5 | 580 | -1.3181 | -1.0819 | [-1.3602, -0.8190] | -0.840378 |
| WAIT_10 | 458 | -1.4222 | -1.2165 | [-1.5971, -0.8882] | -1.044981 |
| WAIT_30 | 289 | -1.6537 | -1.4979 | [-2.4059, -0.8196] | -1.415715 |
| WAIT_60 | 229 | -1.6514 | -1.5138 | [-2.4290, -0.7794] | -1.462836 |
| CURRENT_DIST2_TTL60 | 574 | -1.0649 | -0.8545 | [-0.9941, -0.7183] | -0.592113 |
| CLAIRVOYANT_BEST_FIXED | 284 | 0.6045 | 0.7101 | [0.5734, 0.8276] | 0.838388 |

All executable fixed policies remain negative even under the more optimistic partition-independent upper bound. The clairvoyant row remains an opportunity-level timing upper bound and is not a policy, candidate, validation result, or deployment authorization.

## Formula

The deterministic L2 point sensitivity is:

```text
fee_L2_aggregate_dollars = ceil_to_$0.0001(
  sum_j 0.07 * quantity_j * price_j * (1-price_j)
)
PnL_L2_aggregate = PnL_old + fee_old_whole_cent - fee_L2_aggregate
```

For the actual current contract, each true fill's trade fee is at least its unrounded fee and non-negative rounding residue remains after rebates. Therefore the actual direct net fee is not below the once-per-order aggregate centicent ceiling. Since the sealed old fee is `ceil_to_$0.01(sum raw fee)`, actual PnL improvement is at most `0.99c` per IOC order, regardless of the unobserved fill partition. Maker pairs have no IOC fee and are unchanged.

Official mechanics source: https://docs.kalshi.com/getting_started/fee_rounding (accessed 2026-07-26).

## Superseded v1 wording

The following draft-v1 classifications are withdrawn: `authenticated direct-account centicent fee`, `exact current-account fee repricing`, and `exact direct-centicent fees`. They are replaced by `frozen L2-aggregate lower-fee sensitivity`; only the strict 0.99c upper bound is independent of the missing private fill partition.

## Decision

- `candidate_status = NO_CANDIDATE`
- `deployable = false`
- `live_authorized = false`
- no action, threshold, policy, or oracle source was reselected
