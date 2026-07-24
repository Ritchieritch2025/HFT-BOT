# A01 Path-State Release Lock Closure — Independent Audit

**Audit date:** 2026-07-23  
**Verdict:** **PASS**  
**Scope:** release-lock-only commit `c705ba2a906b72d7512fed65b9179e48a3bb7c34`

This PASS authorizes removal of only
`BLOCK_A01_PATHWISE_FIRST_FILL_CANCEL_ENGINE_MISSING`, because the underlying
state-machine implementation at `7df5923e4cbb0534af911eb216ee84b084bd5128`
already received independent adversarial re-audit PASS at
`c10ad7f401962c1a825c9934266cac33ddbc7507`.

It does **not** make A01 production-ready, authorize live orders, or establish
cash profitability. Four independent production release locks remain active.

## Immutable audit object

| Object | SHA-256 / identity |
|---|---|
| Commit | `c705ba2a906b72d7512fed65b9179e48a3bb7c34` |
| Parent | `a5b092be5bb40ade47c56e9a27ff58a50e123344` |
| Git tree | `350ae05104f8994a534bd63f022fa296db2e2e78` |
| Commit patch | `adf97a97c3886275af852f390549b462839c43f8adb8bd9c0d0e16e05392a475` |
| `a01_materialize_cli.py` | `b5e1a9c63e91a5c13ab6c7d1d879fd476749744b0ccf1fbc624408032ffb9514` |
| `a01_materializer.py` | `0b915b43e4996cd048c1fe3de133947511ca704f42890cead4c320cdc2630f5f` |
| `test_pnl_spine_a01_materializer.py` | `6f2b5bae8e77ecaf66b32c2a5b2aac3b8c8ef7dfbfbdc5c831e77ec95b349b70` |
| Prior state-machine re-audit report | `7f3521d05ef2a3898bc48685a1cb1b11d9ff7039f16fdefbece3e502e47f6b9a` |

The patch touched exactly three files:

```text
tests/test_pnl_spine_a01_materializer.py
tools/research/pnl_spine/a01_materialize_cli.py
tools/research/pnl_spine/a01_materializer.py
```

No runner, ledger, fill engine, production-lineage implementation, authority
schema, or evidence publisher changed.

Both required audit anchors are in the immutable ancestry:

```text
7df5923 -> ancestor of c705ba2: yes
c10ad7f -> ancestor of c705ba2: yes
```

All checks ran from a detached worktree pinned exactly to `c705ba2`, isolating
the result from concurrent main-worktree changes.

## Release-lock delta

The previous CLI tuple contained five explicit production locks. The new tuple
contains four. Independent AST source-segment comparison produced:

```text
old_count = 5
new_count = 4
remaining_four_byte_exact = true
removed_only = true
```

The only removed code and detail mapping is:

```text
BLOCK_A01_PATHWISE_FIRST_FILL_CANCEL_ENGINE_MISSING
runner does not yet implement first-fill stop-new-risk,
sibling/safety-event cancel, reconcile, and residual reduce-only IOC
```

The following four code/detail mappings remain byte-for-byte identical and in
the same order:

1. `BLOCK_A01_LATENCY_FEE_DERIVATION_NOT_BOUND`
   — latency and net margin still must be recomputed from exact measured
   latency and fee-facts receipts.
2. `BLOCK_A01_POINT_IN_TIME_METADATA_INTERVALS_MISSING`
   — lifecycle, tick, and scheduled-start histories still require
   nonoverlapping point-in-time interval materialization.
3. `BLOCK_A01_TRADE_CLOCK_EPOCH_AND_NS_ENGINE_MISSING`
   — receive wall/monotonic epoch reconciliation and nanosecond fill
   boundaries remain incomplete end to end.
4. `BLOCK_A01_OPPORTUNITY_DENOMINATOR_LEDGER_MISSING`
   — every authorized zero-trigger and zero-fill root still must be retained.

## Extraction-spec synchronization

`a01_extraction_spec()["current_real_data_blockers"]` was independently parsed
without importing or executing the audited module.

```text
blocker_count = 21
removed path-state blocker absent = true
remaining release-lock tail exact = true
duplicate blockers = 0
```

The extraction specification therefore removes the same closed path-state
item and retains the same four unresolved release locks as the executable
CLI.

## Synthetic-complete bypass attack

The production-gate CLI test constructs a pinned synthetic bundle that is
complete enough for the materializer to emit nonempty A01 rows and no
data-level blocker. The replay still produced:

```text
receipt.state = BLOCKED
fixture_fragment = null
materialization.rows = nonempty
receipt blocker codes = exactly the four unresolved production gates
```

The relevant control flow remains fail-closed:

```text
if result.ready:
    blockers.extend(UNRESOLVED_PRODUCTION_GATES)
if result.ready and not blockers:
    fixture_fragment = ...

state = READY only if not blockers and fixture_fragment exists
```

Thus even a synthetic input engineered to satisfy every present materializer
field cannot bypass metadata, trade-clock, latency/fee, denominator, or
downstream-lineage readiness. No fixture fragment is emitted while any one of
the four locks remains.

## Authority, raw-byte, and lineage controls

The audited commit does not change authority parsing, the code dependency
closure, raw SHA-256 validation, create-once publication, or the downstream
lineage implementation.

The targeted replay confirms:

- code pins are verified before any input is read;
- a bad raw input SHA remains `BLOCKED`;
- a bad code pin causes zero input reads;
- receipt overwrite is refused;
- a ready materialization still cannot emit a fixture fragment while the four
  release locks exist.

Because no fragment can cross the materializer boundary, the commit cannot
create a lineage-bearing runner input by removing the path-state lock alone.

## Test results

Full PnL-spine replay:

```text
env PYTHONPYCACHEPREFIX=/private/tmp/a01-lock-audit-pycache \
  python3 -m pytest -q tests/test_pnl_spine*.py

327 passed in 3.66s
```

Targeted production-gate and missing-source replay:

```text
test_cli_authority_is_raw_pinned_and_output_is_create_once
test_missing_direct_sources_are_machine_blockers_not_filled_values

2 passed in 0.07s
```

## Conclusion

Commit `c705ba2a906b72d7512fed65b9179e48a3bb7c34` correctly closes only the
already implemented and independently re-audited A01 path-state release lock.
It does not weaken the four remaining production locks or any authority,
raw-byte, fixture, or lineage boundary.

**A01 remains production `BLOCKED` until all four remaining gates are closed
by separately implemented and independently audited commits.**

No implementation file was modified by this audit.
