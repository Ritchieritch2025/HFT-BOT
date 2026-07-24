# A01 Paired State Machine — Independent Adversarial Audit

**Audit date:** 2026-07-23  
**Verdict:** **FAIL — RELEASE BLOCKED**  
**Severity:** **P0**  
**Audit scope:** immutable commit `0f98a71a41d09467b9a40dc07330f64d2ece3629`

## Immutable audit object

| Object | SHA-256 / identity |
|---|---|
| Commit | `0f98a71a41d09467b9a40dc07330f64d2ece3629` |
| Parent | `fff1af04a9895d772e54cf313f87bf6e956abd86` |
| Git tree | `8a1bf0e2efda16360729f5a38c9adcdf214b5ed2` |
| Commit patch | `79f237f9898bec4eca84fca35d3c916c452eb2a5d0d8da275d7429500d4155ab` |
| `runner.py` | `6e5a2e59c8e2cb9dbe97027e6e0ce61e47ae73e042993050e48d10a366d09131` |
| `fills.py` | `d571291e24b1407c8a686a6e811bfad9ff96ae5fb2362ba709b055de6493f201` |
| `ledger.py` | `2a8bd913bb18ac44509557c7a169897856a8cdf21d2e4694128abaece2dba000` |
| `test_pnl_spine_runner.py` | `23620ce427569f89aa8f9d9eb431ed08f2308162087027c5c7de4aba5a30673c` |
| `test_pnl_spine_fills.py` | `bc70b6fb4ee0eee9da614a344b243cce11929e70d84348d831d9abf8f4b666b0` |
| Frozen experiment authority | `3acec9c7b358bf46c377186447ee6e6d28a83d9be7d2bd26983c001cfe168e2f` |

The supplied patch digest was independently reproduced with:

```text
git show --format= --no-ext-diff --binary 0f98a71 | shasum -a 256
79f237f9898bec4eca84fca35d3c916c452eb2a5d0d8da275d7429500d4155ab
```

## Test result

```text
env PYTHONPYCACHEPREFIX=/private/tmp/pycache \
  python3 -m pytest -q tests/test_pnl_spine*.py

293 passed in 3.55s
```

The green suite is genuine, but it does not exercise the two causal daily-loss attacks below through the new A01 paired-path branch.

## P0-01 — Partial IOC loss is hidden until later settlement

The new A01 path records one `realized_at_ns` equal to the **last cashflow on the entire path**:

- `runner.py:3101-3103`
- `runner.py:3124-3126`

The A01 risk pass then consumes only the final path result and that one timestamp:

- `runner.py:3538-3557`
- `runner.py:3563-3577`

It does not emit or replay the FIFO realized-PnL slices already implemented by `_lot_matched_realized_pnl` at `runner.py:1866-1953`. Consequently, a loss realized by a partial IOC is invisible to the daily-loss gate until a later residual settlement.

### Adversarial replay

Using the repository's fully bound `base_fixture` authority:

1. First A01 path buys one YES-equivalent contract at `0.39`.
2. Reduce-only IOC sells `0.5` contract at `0.10`.
3. This realizes at least `-$0.145` gross immediately, exceeding a `max_daily_loss_e6=100000` gate even before fees.
4. The remaining `0.5` contract settles YES at `+20s`.
5. A second A01 decision is submitted at `+10s`, after the partial IOC loss but before settlement.

Observed immutable-HEAD result:

```text
state = NET_PNL_COMPLETE
blockers = []
totals = {
  gross_pnl_e6: 190000,
  fee_cost_e6: 20300,
  variable_cost_e6: 0,
  net_pnl_e6: 169700
}

first path:
  closure_state = CLOSED_BY_SETTLEMENT
  final net_pnl_e6 = 156800
  realized_at_ns = decision + 20s

second path:
  PATH_COMPLETE
  net_pnl_e6 = 12900
```

This is an invalid completed PnL path: the second order would have been forbidden once the earlier partial IOC realized the daily stop loss. A later settlement profit cannot retroactively authorize that order.

## P0-02 — Loss exactly at the next decision timestamp is ignored

The frozen causal risk ordering is closure/realized PnL before a new reservation at an equal timestamp. The existing risk engine states this explicitly and its test verifies:

```text
EXIT, REALIZED_PNL, RESERVE, FILL
```

The new A01 risk pass instead includes prior PnL only when:

```python
realized_at_ns < row.decision_ts_ns
```

at `runner.py:3567`. Equality is silently treated as if the loss were not yet known.

### Adversarial replay

1. First A01 path realizes `net_pnl_e6=-296300`.
2. Second decision timestamp is set exactly equal to the first path's IOC realization timestamp.
3. Daily-loss limit is `100000`.

Observed immutable-HEAD result:

```text
state = NET_PNL_COMPLETE
blockers = []
totals.net_pnl_e6 = -283400

first path realized_at_ns == second decision_ts_ns
second path PATH_COMPLETE with net_pnl_e6 = 12900
```

This contradicts the repository's established equal-timestamp causal priority and violates fail-closed risk admission.

## Other attacked invariants

The following reviewed behaviors were correctly implemented in this commit:

- one A01 decision produces one strategy path and one no-trade baseline;
- both passive GTC legs share one activation and safety timeout;
- the first partial fill starts cancellation of both own remainder and sibling;
- public prints at the rounded cancel-effective boundary remain eligible;
- a public trade ID is not reused across legs or decisions;
- YES and complementary NO fills reconcile in one exact YES-equivalent ledger;
- a net-zero pair closes without a fabricated IOC;
- a nonzero position exits at cancel-effective plus measured IOC p99 using the latest qualified full-depth L2 snapshot;
- an IOC residual requires exact finalized settlement or blocks;
- order quantity, source quantity, cash, fee, position, and collateral identities are checked;
- A11 remains blocked on its exact cancel-stream prerequisite;
- B09 remains blocked on its training prerequisite.

These passes do not offset the P0 risk-admission failures.

## Required repair and acceptance

Do not remove the A01 production lock and do not publish A01 cash PnL from this commit.

The repair must:

1. replay A01 as one paired decision/reservation while preserving causal cashflow slices;
2. attribute realized PnL separately at every IOC fill and later settlement;
3. process equal-time `EXIT/SETTLEMENT`, then `REALIZED_PNL`, then new `RESERVE`, matching the established risk engine;
4. fail closed if a paired-path slice cannot reconcile exactly to final path PnL;
5. add both attacks above as end-to-end A01 regression tests, each requiring `PNL_BLOCKED`, `totals=null`, and `RISK_DAILY_LOSS_CHANGED_ADMISSION`;
6. rerun the complete `tests/test_pnl_spine*.py` suite and obtain an independent re-audit of the repaired immutable HEAD.

No implementation file was modified by this audit.
