# A01 Paired State Machine — Independent Adversarial Re-audit

**Re-audit date:** 2026-07-23  
**Verdict:** **PASS**  
**Scope:** realized-PnL chronology repair at immutable commit `7df5923e4cbb0534af911eb216ee84b084bd5128`

This PASS closes the two P0 findings in
`A01_PAIRED_STATE_MACHINE_INDEPENDENT_AUDIT_2026-07-23.md`. It does not
authorize live orders, remove unrelated A01 production-data gates, or promote
engineering replay results to a profitability claim.

## Immutable audit object

| Object | SHA-256 / identity |
|---|---|
| Commit | `7df5923e4cbb0534af911eb216ee84b084bd5128` |
| Parent | `06b30c7072ad4a31bc22bf424030aa7b9eb6bfaf` |
| Git tree | `a35616d5e78eba4a79b99594343521ddb8f77057` |
| Commit patch | `ece8077cccb8c23e88f0f4d76d81b8a2081feb079ff0228ff7aea4e03fa960f9` |
| `runner.py` | `c1057b4a416df115b9b63193542cd7e3309af53694a96c73201795f2a104fea6` |
| `test_pnl_spine_runner.py` | `a6a78c7ec3dd51657466b64eea2555ae747ac0cc27728793c0228fa54981fef5` |
| Unchanged `fills.py` | `d571291e24b1407c8a686a6e811bfad9ff96ae5fb2362ba709b055de6493f201` |
| Unchanged `ledger.py` | `2a8bd913bb18ac44509557c7a169897856a8cdf21d2e4694128abaece2dba000` |
| Frozen experiment authority | `3acec9c7b358bf46c377186447ee6e6d28a83d9be7d2bd26983c001cfe168e2f` |

The patch digest was independently reproduced with:

```text
git show --format= --no-ext-diff --binary 7df5923 | shasum -a 256
ece8077cccb8c23e88f0f4d76d81b8a2081feb079ff0228ff7aea4e03fa960f9
```

All execution was performed from a detached worktree pinned to the audited
commit, so concurrent latency-producer changes in the main worktree could not
affect the result.

## Complete test result

```text
env PYTHONPYCACHEPREFIX=/private/tmp/a01-reaudit-pycache \
  python3 -m pytest -q tests/test_pnl_spine*.py

324 passed in 4.21s
```

The four chronology and legacy-risk tests were also replayed separately:

```text
test_partial_ioc_loss_blocks_before_later_profitable_settlement
test_equal_timestamp_ioc_loss_precedes_next_reserve_admission
test_partial_exit_realizes_loss_before_later_settlement
test_risk_replay_orders_equal_timestamp_closure_before_new_fill

4 passed in 0.05s
```

## Replayed P0-01 — partial IOC loss before later settlement

Attack:

1. Entry buys one YES-equivalent contract at `0.39`.
2. A reduce-only IOC exits `0.5` contract at `0.10`.
3. The partial IOC realizes more than `-$0.10` before fees.
4. Residual inventory later settles profitably at `+20s`.
5. A second opportunity attempts admission at `+10s`.
6. `max_daily_loss_e6=100000`.

Result:

```text
state = PNL_BLOCKED
totals = null
blocker = RISK_DAILY_LOSS_CHANGED_ADMISSION
```

The IOC principal and fee are now realized at the IOC cashflow timestamp. The
later positive settlement receives its own later slice and cannot
retroactively authorize the intervening order.

## Replayed P0-02 — equal-timestamp loss before reserve

Attack:

1. First path realizes an IOC loss.
2. The next decision timestamp equals that IOC realization timestamp exactly.
3. The loss exceeds the daily-loss limit.

Result:

```text
state = PNL_BLOCKED
totals = null
blocker = RISK_DAILY_LOSS_CHANGED_ADMISSION
```

The repaired event order is:

```text
EXIT/SETTLEMENT priority 10
REALIZED_PNL     priority 20
RESERVE          priority 30
```

Therefore equal-timestamp exit or settlement cash is applied before new risk
admission.

An additional settlement-only boundary attack used an empty IOC bid book, a
final NO payout, and a second decision exactly at settlement time. It also
returned `PNL_BLOCKED` with `RISK_DAILY_LOSS_CHANGED_ADMISSION`; the settlement
slice was `-390000 e6` at the exact reserve timestamp.

## Fee, variable-cost, FIFO, threshold, and conservation attacks

### Fees

An end-to-end case made the principal loss `-90000 e6`, below the `100000 e6`
limit, while the same IOC charged `-14700 e6`. Only principal plus fee crosses
the limit.

Observed result:

```text
principal realized = -90000 e6
fee realized       = -14700 e6
state              = PNL_BLOCKED
blocker            = RISK_DAILY_LOSS_CHANGED_ADMISSION
```

This proves fees are not deferred to settlement or omitted from daily loss.

### Variable cost and cashflow time

A direct immutable-ledger replay produced:

```text
t=10  entry principal  deferred basis = -400000 e6
t=10  entry fee        realized       =   -5000 e6
t=20  exit principal   realized       =  150000 e6
t=20  exit fee         realized       =  -17400 e6
t=30  variable cost    realized       =   -2500 e6
slice total                           =  125100 e6
final net PnL                         =  125100 e6
```

The fee total, variable-cost total, gross total, and net total each reconcile
independently.

### FIFO

Two entry lots were opened:

```text
lot 1 basis = -80000 e6
lot 2 basis = -480000 e6
```

A partial exit consumed `-260000 e6` in FIFO order and realized `90000 e6`.
Final settlement consumed the remaining `-300000 e6` basis and realized
`200000 e6`. The slice total and final gross/net result were both `290000 e6`.
No basis or position remained.

### Exact `>=` daily-loss threshold

The audited runner produced a first-path final loss of exactly `-296300 e6`.
With `max_daily_loss_e6=296300`, the subsequent opportunity was rejected:

```text
state = PNL_BLOCKED
totals = null
blocker = RISK_DAILY_LOSS_CHANGED_ADMISSION
```

The equality boundary is enforced.

### Tamper and conservation

Two independent result-authority mutations were rejected:

```text
net total changed by 1 e6
  -> A01 realized-PnL slices do not reconcile to final net PnL

fee and variable-cost totals cross-shifted by 1 e6
  -> A01 fee slices do not reconcile at cashflow time
```

Code inspection also confirmed that replay refuses:

- a closing cashflow exceeding FIFO position basis;
- non-exact MoneyE6 partial-basis allocation;
- retained FIFO quantity or cash basis;
- mismatched fee, variable-cost, gross, or net totals;
- a slice SHA or causal-priority mismatch.

## Code-level conclusion

The repaired implementation no longer uses one final path timestamp for daily
loss. It derives source-bound slices from the immutable ledger cashflows,
tracks opening basis separately from realized cash, emits fees and variable
costs at their own timestamps, consumes FIFO basis on every partial close, and
replays all realized slices before equal-time reserve events.

No P0 or P1 defect was found in the audited repair scope. The earlier
chronology release block may be closed for commit
`7df5923e4cbb0534af911eb216ee84b084bd5128`.

No implementation file was modified by this re-audit.
