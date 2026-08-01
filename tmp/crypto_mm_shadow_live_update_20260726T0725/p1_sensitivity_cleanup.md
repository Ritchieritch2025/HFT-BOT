# P1 fixed-cohort sensitivity cleanup

Status: **post-hoc sensitivity only; not validation and not candidate
selection**.

## Common cohort

- Source clean cycles: 84
- External-probe cooldown: 60s after the
  final non-shadow fill
- Cooldown exclusions: [37, 38]
- Common episode count for every reported policy:
  **82**

No policy drops a missing horizon row. If the causal quote is missing/stale,
the row uses the observed original episode P&L. If touch depth is below one,
the row uses the worse of that original P&L and the modeled current touch+1c
IOC with exact centicent fees. This convention is deliberately pessimistic
and post-hoc; it is not proof that the IOC would execute.

## Fixed conservative exit horizons

| horizon | n | fail-closed rows | total USD | mean/cycle | market-cluster CI |
| ---: | ---: | ---: | ---: | ---: | --- |
| 0.25 | 82 | 1 | -2.2867 | -2.7887c | [-3.4705, -2.3069]c |
| 0.5 | 82 | 1 | -2.1599 | -2.6340c | [-3.3074, -2.3030]c |
| 1 | 82 | 1 | -2.4265 | -2.9591c | [-3.9133, -2.4703]c |
| 2 | 82 | 0 | -2.2564 | -2.7517c | [-3.1038, -2.3943]c |
| 5 | 82 | 0 | -1.7768 | -2.1668c | [-2.7013, -1.6939]c |
| 10 | 82 | 0 | -1.9224 | -2.3444c | [-3.1559, -1.7156]c |
| 30 | 82 | 0 | -2.8417 | -3.4655c | [-4.8999, -2.1484]c |
| 60 | 82 | 1 | -3.3280 | -4.0585c | [-6.0584, -1.9527]c |

## Locked state-rule sensitivities

Rules are listed in their fixed declaration order. They were not searched or
ranked in this run. A missing state feature defaults to the rule's short
horizon.

| rule | n | missing-state defaults | fail-closed rows | total USD | mean/cycle | market-cluster CI |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `prior_screen_pair_profit_le_1c` | 82 | 7 | 1 | -1.6727 | -2.0399c | [-2.7247, -1.4682]c |
| `queue_proxy_eta_le_5s` | 82 | 12 | 0 | -1.9720 | -2.4049c | [-2.9792, -2.0303]c |
| `opposite_flow_10s_ge_2500` | 82 | 7 | 0 | -1.7203 | -2.0979c | [-2.6250, -1.5038]c |

## Verdict

All reported fixed horizons and all three locked state rules remain negative on the common episode set.

The intervals use only eight 15-minute market clusters and remain descriptive.
The roughly 1 Hz quote receipts still cannot establish executable sub-second
touch prices.
